import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.bootstrap import ConfigSecurityError, validate_settings
from app.config import Settings, get_settings
from app.domain.candle_utils import detect_gaps, interval_ms, is_candle_closed, split_closed_and_forming
from app.domain.candles import Candle
from app.models import SignalStatus
from app.services.execution_model import ExecutionModel, ExitOutcome
from app.services.market_data import LiveQuote, MarketDataService
from app.services.telegram_approval import TelegramApprovalService
from app.services.telegram_notifier import TelegramNotifier
from app.services.signal_lifecycle import SignalLifecycleService
from app.services.signal_monitor import SignalMonitorService
from tests.helpers.factories import make_signal


def test_empty_telegram_allowlist_fail_closed():
    settings = Settings(
        telegram_allowed_user_ids="",
        dashboard_api_key="a" * 20,
        admin_api_key="b" * 20,
    )
    svc = TelegramApprovalService(settings)
    assert svc.user_allowed(111, "telegram") is False


def test_config_requires_api_keys(monkeypatch):
    monkeypatch.delenv("SKIP_CONFIG_GUARD", raising=False)
    with pytest.raises(ConfigSecurityError):
        validate_settings(Settings(dashboard_api_key="", admin_api_key=""))


@pytest.mark.asyncio
async def test_dashboard_cannot_approve(client, session):
    sig = make_signal()
    session.add(sig)
    await session.commit()
    resp = await client.post(
        "/api/signals/telegram-approval",
        json={"public_id": sig.public_id, "approved": True},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_can_approve(admin_client, session):
    sig = make_signal()
    session.add(sig)
    await session.commit()
    resp = await admin_client.post(
        "/api/signals/telegram-approval",
        json={"public_id": sig.public_id, "approved": True},
    )
    assert resp.status_code == 200
    await session.refresh(sig)
    assert sig.telegram_approved is True


@pytest.mark.asyncio
async def test_forged_telegram_webhook_rejected():
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/telegram/webhook/" + ("y" * 32),
            json={"callback_query": {"id": "1"}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )
        assert resp.status_code in (403, 503)


@pytest.mark.asyncio
async def test_concurrent_approval_idempotent(session):
    sig = make_signal()
    session.add(sig)
    await session.commit()
    svc = TelegramApprovalService()
    r1 = await svc.approve(session, sig.public_id, 111)
    r2 = await svc.approve(session, sig.public_id, 111)
    assert r1[1] is None
    assert r2[1] == "ALREADY_APPROVED"


@pytest.mark.asyncio
async def test_late_approval_after_entry_touched(session):
    sig = make_signal(telegram_approved=None)
    session.add(sig)
    await session.commit()
    bar = Candle(open=100, high=101, low=99, close=100, time_ms=1_700_000_000_000)
    svc = TelegramApprovalService()
    _, err = await svc.approve(session, sig.public_id, 111, pre_approval_bars=[bar])
    await session.refresh(sig)
    assert err == "LATE_APPROVAL_ENTRY_TOUCHED"
    assert sig.status == SignalStatus.MISSED_ENTRY.value


def test_detect_5m_gap():
    step = interval_ms("5m")
    c1 = Candle(1, 1, 1, 1, 0)
    c2 = Candle(1, 1, 1, 1, step * 2)
    gaps = detect_gaps([c1, c2], "5m")
    assert gaps == [step]


def test_no_double_exit_cost():
    model = ExecutionModel()
    entry_fill = model.open_fill(100, "LONG", "LIMIT")
    raw_exit = 106
    exit_fill = model.close_fill(raw_exit, "LONG", ExitOutcome.TP)
    pnl = model.net_pnl("LONG", entry_fill, exit_fill)
    again = model.net_pnl("LONG", entry_fill, exit_fill)
    assert pnl == again


def test_long_short_rr_consistency():
    model = ExecutionModel()
    long_rr = model.calc_rr("LONG", 100, 98, 106, "LIMIT", 1.0)
    short_rr = model.calc_rr("SHORT", 100, 102, 94, "LIMIT", 1.0)
    assert long_rr.net_rr > 0
    assert short_rr.net_rr > 0
    assert abs(long_rr.net_rr - short_rr.net_rr) < 0.05


def test_stop_and_confirmation_entry_types():
    life = SignalLifecycleService()
    sig = make_signal(entry_type="STOP")
    ok, _ = life.entry_triggered(sig, 101, 99, 100)
    assert ok
    sig.entry_type = "CONFIRMATION"
    ok2, _ = life.entry_triggered(sig, 100.5, 99, 100.2)
    assert ok2


def test_sl_first_when_both_hit():
    life = SignalLifecycleService()
    sig = make_signal(status=SignalStatus.ACTIVE.value)
    status, raw, note = life.resolve_active_bar(sig, 107, 97, 100)
    assert status == SignalStatus.SL_HIT.value
    assert note == "SL_FIRST_AMBIGUOUS"


def test_stale_quote_flag():
    md = MarketDataService()
    old = LiveQuote(1.0, datetime.now(timezone.utc) - timedelta(minutes=10), 0, True, "t")
    assert md.is_quote_stale(old) is True


def test_forming_candle_split():
    now_ms = 1_000_000
    step = interval_ms("5m")
    closed = Candle(1, 1, 1, 1, now_ms - step * 2)
    forming = Candle(1, 1, 1, 1, now_ms - 1000)
    c, f = split_closed_and_forming([closed, forming], "5m", now_ms)
    assert len(c) == 1
    assert f is not None
    assert is_candle_closed(closed, "5m", now_ms) is True


def test_public_chart_url_blocks_localhost():
    n = TelegramNotifier(Settings(dashboard_public_url="http://127.0.0.1:8000"))
    assert n.public_chart_url("00000000-0000-4000-8000-000000000001") is None
    n2 = TelegramNotifier(Settings(dashboard_public_url="https://trade.example.com"))
    assert n2.public_chart_url("00000000-0000-4000-8000-000000000001") is not None


@pytest.mark.asyncio
async def test_paper_lifecycle_deterministic(session):
    sig = make_signal(
        telegram_approved=True,
        telegram_approved_at=datetime.fromtimestamp(1_700_000_000, tz=timezone.utc),
    )
    sig.last_processed_candle_ms = 0
    session.add(sig)
    await session.commit()

    class FakeMarket:
        async def fetch_live_price(self, symbol):
            return LiveQuote(100, datetime.now(timezone.utc), int(datetime.now(timezone.utc).timestamp() * 1000), False, "test")

        async def fetch_kline_bundle(self, symbol, interval, limit=200):
            from app.services.market_data import KlineBundle

            bar = Candle(open=100, high=100.5, low=99.5, close=100.2, time_ms=1_700_000_300_000)
            return KlineBundle(closed=[bar], forming=None, gaps=[], interval="5m")

        def is_quote_stale(self, q):
            return False

    monitor = SignalMonitorService(FakeMarket(), SignalLifecycleService())
    from app.models import RuntimeSettings

    runtime = RuntimeSettings(id=1, max_pending_minutes=90, max_missed_entry_pct=1, holding_time_hours=6, min_net_rr=2)
    out = await monitor.monitor_open_signals(session, runtime)
    await session.refresh(sig)
    assert sig.public_id in out
    assert sig.status == SignalStatus.ACTIVE.value
