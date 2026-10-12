from datetime import datetime, timezone
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest
from sqlalchemy import select

from app.domain.approval_policy import entry_monitor_start_ms
from app.domain.candles import Candle
from app.models import NotificationOutbox, SignalStatus, SystemState
from app.services.notification_outbox import NotificationOutboxService
from app.services.system_state import try_acquire_worker_lock
from app.services.telegram_approval import TelegramApprovalService
from app.services.telegram_bot import TelegramBotService
from app.services.market_data import KlineBundle, LiveQuote, MarketDataService
from app.services.signal_monitor import SignalMonitorService
from app.services.signal_lifecycle import SignalLifecycleService
from tests.helpers.factories import make_signal
from tests.helpers.market_fakes import EmptyCompleteMarket, PrecheckTouchMarket, complete_bundle, live_quote


@pytest.mark.asyncio
async def test_approval_rejects_closed_bar_entry_touch(session):
    touch_ms = 1_700_000_000_000
    sig = make_signal(
        created_at=datetime.fromtimestamp((touch_ms - 600_000) / 1000, tz=timezone.utc),
    )
    session.add(sig)
    await session.commit()
    bar = Candle(open=100, high=101, low=99, close=100, time_ms=touch_ms)
    svc = TelegramApprovalService(market=PrecheckTouchMarket(bar))
    _, err = await svc.approve(session, sig.public_id, 111)
    await session.refresh(sig)
    assert err == "LATE_APPROVAL_ENTRY_TOUCHED"
    assert sig.status == SignalStatus.MISSED_ENTRY.value


@pytest.mark.asyncio
async def test_approval_sets_entry_monitor_from_ms(session):
    approval_ms = 1_700_000_305_000
    sig = make_signal(
        created_at=datetime.fromtimestamp(1_700_000_000, tz=timezone.utc),
    )
    session.add(sig)
    await session.commit()
    svc = TelegramApprovalService(market=EmptyCompleteMarket())
    fixed = datetime.fromtimestamp(approval_ms / 1000, tz=timezone.utc)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz else fixed.replace(tzinfo=None)

    with patch("app.services.telegram_approval.datetime", _FixedDatetime):
        with patch("app.services.telegram_approval.time.time", return_value=approval_ms / 1000 + 120):
            _, err = await svc.approve(session, sig.public_id, 111)
    assert err is None
    await session.refresh(sig)
    assert sig.entry_monitor_from_ms == entry_monitor_start_ms(approval_ms, "5m")


@pytest.mark.asyncio
async def test_monitor_respects_entry_monitor_from_ms(session):
    monitor_from = 1_700_000_600_000
    sig = make_signal(
        telegram_approved=True,
        telegram_approved_at=datetime.fromtimestamp(1_700_000_305, tz=timezone.utc),
        entry_monitor_from_ms=monitor_from,
        last_processed_candle_ms=0,
    )
    session.add(sig)
    await session.commit()
    monitor = SignalMonitorService(EmptyCompleteMarket(), SignalLifecycleService())
    early = Candle(100, 100.5, 99.5, 100, 1_700_000_300_000)
    late = Candle(100, 100.5, 99, 100, monitor_from)
    bars = monitor._eligible_closed_bars(sig, [early, late])
    assert early not in bars
    assert late in bars


@pytest.mark.asyncio
async def test_poll_once_does_not_advance_offset_on_failure():
    bot = TelegramBotService()

    class FakeResp:
        def json(self):
            return {"result": [{"update_id": 10, "x": 1}]}

    with patch.object(bot, "handle_update", AsyncMock(side_effect=RuntimeError("boom"))):
        with patch("httpx.AsyncClient.get", AsyncMock(return_value=FakeResp())):
            with patch.object(TelegramBotService, "polling_enabled", new_callable=PropertyMock, return_value=True):
                start = await bot._load_offset()
                await bot.poll_once()
                end = await bot._load_offset()
    assert start == end


@pytest.mark.asyncio
async def test_poll_once_advances_after_success():
    bot = TelegramBotService()

    class FakeResp:
        def json(self):
            return {"result": [{"update_id": 5}]}

    with patch.object(bot, "handle_update", AsyncMock()):
        with patch("httpx.AsyncClient.get", AsyncMock(return_value=FakeResp())):
            with patch.object(TelegramBotService, "polling_enabled", new_callable=PropertyMock, return_value=True):
                await bot.poll_once()
    assert await bot._load_offset() == 6


@pytest.mark.asyncio
async def test_worker_lock_exclusive_two_sessions():
    from app.db import SessionLocal

    async with SessionLocal() as s1:
        ok1 = await try_acquire_worker_lock(s1, "worker-a", ttl_seconds=60)
    async with SessionLocal() as s2:
        ok2 = await try_acquire_worker_lock(s2, "worker-b", ttl_seconds=60)
    assert ok1 is True
    assert ok2 is False


@pytest.mark.asyncio
async def test_worker_lock_released_after_expiry(session):
    state = SystemState(id=1, worker_lock_owner="old", worker_lock_until_ms=1)
    session.add(state)
    await session.commit()
    ok = await try_acquire_worker_lock(session, "worker-new", ttl_seconds=60)
    assert ok is True


@pytest.mark.asyncio
async def test_outbox_idempotent_enqueue(session):
    sig = make_signal()
    session.add(sig)
    await session.commit()
    outbox = NotificationOutboxService()
    r1 = await outbox.enqueue(session, "NEW:x", "NEW", sig)
    r2 = await outbox.enqueue(session, "NEW:x", "NEW", sig)
    await session.commit()
    assert r1.id == r2.id
    rows = (await session.execute(select(NotificationOutbox))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_outbox_marks_sent_only_after_success(session):
    sig = make_signal()
    session.add(sig)
    await session.commit()
    outbox = NotificationOutboxService()
    await outbox.enqueue(session, f"ACTIVE:{sig.public_id}", "ACTIVE", sig)
    await session.commit()
    with patch.object(type(outbox.notifier), "enabled", new_callable=PropertyMock, return_value=True):
        with patch.object(outbox.notifier, "notify_entry_activated", AsyncMock(side_effect=RuntimeError("api down"))):
            sent = await outbox.flush_pending(session)
            assert sent == 0
    row = (await session.execute(select(NotificationOutbox))).scalar_one()
    assert row.status == "pending"
    assert row.attempts == 1


@pytest.mark.asyncio
async def test_incomplete_history_sets_data_error(session):
    sig = make_signal(telegram_approved=True, entry_monitor_from_ms=0)
    session.add(sig)
    await session.commit()

    class GapMarket:
        async def fetch_live_price(self, symbol):
            return live_quote()

        async def fetch_kline_bundle(self, symbol, interval, limit=200):
            return KlineBundle(
                closed=[],
                forming=None,
                gaps_detected=[1000],
                gaps_remaining=[1000],
                data_complete=False,
                interval="5m",
            )

        def is_quote_stale(self, q):
            return False

    from app.models import RuntimeSettings

    runtime = RuntimeSettings(id=1, max_pending_minutes=90, max_missed_entry_pct=0.3, holding_time_hours=6, min_net_rr=2)
    monitor = SignalMonitorService(GapMarket(), SignalLifecycleService())
    await monitor.monitor_open_signals(session, runtime)
    await session.refresh(sig)
    assert sig.status == SignalStatus.DATA_ERROR.value


def test_live_quote_stale_by_receipt_age():
    md = MarketDataService()
    old_receipt = int(datetime.now(timezone.utc).timestamp() * 1000) - 600_000
    q = LiveQuote(1.0, datetime.now(timezone.utc), old_receipt, None, False, "t")
    assert md.is_quote_stale(q) is True

