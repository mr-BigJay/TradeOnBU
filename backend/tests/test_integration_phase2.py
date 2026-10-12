import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.domain.candles import Candle
from app.models import RuntimeSettings, SignalStatus
from app.services.market_data import KlineBundle, LiveQuote
from app.services.paper_execution import PaperExecutionEngine
from app.services.signal_lifecycle import SignalLifecycleService
from app.services.signal_monitor import SignalMonitorService
from app.services.telegram_approval import TelegramApprovalService
from tests.helpers.factories import make_signal


@pytest.mark.asyncio
async def test_unauthorized_telegram_user_rejected(session):
    sig = make_signal()
    session.add(sig)
    await session.commit()
    svc = TelegramApprovalService()
    approved, err = await svc.approve(session, sig.public_id, user_id=999999)
    assert approved is None
    assert err == "UNAUTHORIZED_USER"


@pytest.mark.asyncio
async def test_duplicate_approval_single_event(session):
    sig = make_signal()
    session.add(sig)
    await session.commit()
    svc = TelegramApprovalService()
    lifecycle = SignalLifecycleService()
    first, err1 = await svc.approve(session, sig.public_id, user_id=111)
    assert err1 is None
    second, err2 = await svc.approve(session, sig.public_id, user_id=111)
    assert err2 == "ALREADY_APPROVED"
    from sqlalchemy import select
    from app.models import SignalEvent

    events = (
        await session.execute(
            select(SignalEvent).where(SignalEvent.signal_id == sig.id, SignalEvent.event_type == "TELEGRAM_APPROVED")
        )
    ).scalars().all()
    assert len(events) == 1


@pytest.mark.asyncio
async def test_entry_not_active_before_approval(session):
    sig = make_signal(telegram_approved=None)
    session.add(sig)
    await session.commit()
    lifecycle = SignalLifecycleService()
    triggered, _ = lifecycle.entry_triggered(sig, 99.0, 100.0, 100.0)
    assert triggered is True
    monitor = SignalMonitorService(market=_FakeMarket(), lifecycle=lifecycle)
    bars = [_bar(99, 100.5, 98.5, 100.0, 1_700_000_000_000)]
    ev = await monitor.process_waiting(session, sig, 0.3, 100.0, bars, False)
    assert "ACTIVE" not in ev
    assert sig.status == SignalStatus.WAITING_FOR_ENTRY.value


@pytest.mark.asyncio
async def test_unapproved_signal_expires(session):
    sig = make_signal(
        telegram_approved=None,
        pending_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    session.add(sig)
    await session.commit()
    monitor = SignalMonitorService(market=_FakeMarket(), lifecycle=SignalLifecycleService())
    runtime = RuntimeSettings(id=1, max_pending_minutes=90, max_missed_entry_pct=0.3, holding_time_hours=6, min_net_rr=2)
    await monitor.monitor_open_signals(session, runtime)
    await session.refresh(sig)
    assert sig.status == SignalStatus.EXPIRED.value


@pytest.mark.asyncio
async def test_stale_data_no_trade_outcome(session):
    sig = make_signal(telegram_approved=True, status=SignalStatus.ACTIVE.value, activated_at=datetime.now(timezone.utc))
    sig.fill_price = 100.0
    session.add(sig)
    await session.commit()
    monitor = SignalMonitorService(market=_FakeMarket(), lifecycle=SignalLifecycleService())
    ev = await monitor.process_active(session, sig, [_bar(105, 106, 99, 102, 1_700_000_000_000)], 100.0, True)
    assert ev == []
    assert sig.status == SignalStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_candle_cursor_prevents_reprocessing(session):
    sig = make_signal(
        telegram_approved=True,
        telegram_approved_at=datetime.fromtimestamp(0, tz=timezone.utc),
        last_processed_candle_ms=1_700_000_000_000,
    )
    session.add(sig)
    await session.commit()
    monitor = SignalMonitorService(market=_FakeMarket(), lifecycle=SignalLifecycleService())
    bars = monitor._eligible_closed_bars(sig, [_bar(100, 101, 99, 100, 1_700_000_000_000)])
    assert bars == []


@pytest.mark.asyncio
async def test_conservative_sl_when_both_hit(session):
    lifecycle = SignalLifecycleService()
    sig = make_signal(status=SignalStatus.ACTIVE.value)
    sig.fill_price = 100.0
    status, price, note = lifecycle.resolve_active_bar(sig, 107, 97, 100)
    assert status == SignalStatus.SL_HIT.value
    assert note == "SL_FIRST_AMBIGUOUS"


@pytest.mark.asyncio
async def test_no_live_bitunix_order_endpoints(client):
    resp = await client.get("/api/health")
    body = resp.json()
    assert body["live_trading"] is False
    assert body["paper_only"] is True


@pytest.mark.asyncio
async def test_net_rr_with_fees():
    paper = PaperExecutionEngine()
    rr = paper.calc_rr("LONG", 100, 98, 106, "LIMIT", 1.0)
    assert rr.net_rr == round(rr.net_reward_usdt / abs(rr.net_loss_usdt), 3)


def _bar(o, h, l, c, t):
    return Candle(open=o, high=h, low=l, close=c, time_ms=t)


class _FakeMarket:
    async def fetch_live_price(self, symbol):
        return LiveQuote(
            100.0,
            datetime.now(timezone.utc),
            int(datetime.now(timezone.utc).timestamp() * 1000),
            False,
            "test",
        )

    async def fetch_kline_bundle(self, symbol, interval, limit=200):
        return KlineBundle(
            closed=[_bar(100, 101, 99, 100, 1_700_000_000_000)],
            forming=None,
            gaps=[],
            interval="5m",
        )

    def is_quote_stale(self, quote):
        return False
