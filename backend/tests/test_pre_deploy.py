from datetime import datetime, timezone
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest
from sqlalchemy import select

from app.domain.candles import Candle
from app.models import NotificationOutbox, RuntimeSettings, SignalStatus
from app.services.approval_precheck import ApprovalPrecheckService
from app.services.market_data import KlineBundle
from app.services.notification_outbox import NotificationOutboxService
from app.services.signal_lifecycle import SignalLifecycleService
from app.services.signal_monitor import SignalMonitorService
from app.services.telegram_notifier import TelegramNotifier
from tests.helpers.factories import make_signal
from tests.helpers.market_fakes import complete_bundle, live_quote


@pytest.mark.asyncio
async def test_approval_rejects_touch_in_creation_partial_5m():
    step = 5 * 60_000
    creation_open = 1_700_000_000_000
    created_ms = creation_open + 2 * 60_000
    approval_ms = creation_open + 4 * 60_000
    touch_1m = Candle(open=100, high=101, low=99, close=100, time_ms=created_ms + 60_000)

    class BoundaryMarket:
        async def fetch_kline_bundle(self, symbol, interval, limit=200):
            if interval == "1m":
                return complete_bundle(closed=[touch_1m], interval="1m")
            return complete_bundle(
                closed=[Candle(100, 100, 100, 100, creation_open)],
                interval="5m",
            )

    sig = make_signal(created_at=datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc))
    pre = ApprovalPrecheckService(market=BoundaryMarket())
    result = await pre.evaluate(sig, approval_ms, approval_ms + 60_000)
    assert result.ok is False
    assert result.reason == "LATE_APPROVAL_ENTRY_TOUCHED"


@pytest.mark.asyncio
async def test_data_error_recovers_active_without_second_entry(session):
    sig = make_signal(
        status=SignalStatus.DATA_ERROR.value,
        telegram_approved=True,
        entry_monitor_from_ms=1_700_000_000_000,
        fill_price=100.0,
        entry_activation_candle_ms=1_700_000_000_000,
        activated_at=datetime.fromtimestamp(1_700_000_300, tz=timezone.utc),
        last_processed_candle_ms=1_700_000_000_000,
        analysis={"pre_data_error_status": SignalStatus.ACTIVE.value},
    )
    session.add(sig)
    await session.commit()

    bars = [
        Candle(100, 100.5, 99.5, 100, 1_700_000_300_000),
        Candle(100, 107, 97, 100, 1_700_000_600_000),
    ]

    class HistMarket:
        async def fetch_live_price(self, symbol):
            return live_quote()

        async def fetch_historical_closed(self, symbol, interval, from_ms, max_bars=500):
            return complete_bundle(closed=[b for b in bars if b.time_ms >= from_ms], interval="5m")

        async def fetch_kline_bundle(self, symbol, interval, limit=200):
            return complete_bundle(closed=bars, interval="5m")

        def is_quote_stale(self, q):
            return False

    runtime = RuntimeSettings(id=1, max_pending_minutes=90, max_missed_entry_pct=0.3, holding_time_hours=6, min_net_rr=2)
    monitor = SignalMonitorService(HistMarket(), SignalLifecycleService())
    out = await monitor.monitor_open_signals(session, runtime)
    await session.refresh(sig)
    assert sig.status == SignalStatus.SL_HIT.value
    assert "ACTIVE" not in str(out)


@pytest.mark.asyncio
async def test_replay_chronological_after_gap(session):
    sig = make_signal(
        telegram_approved=True,
        entry_monitor_from_ms=1_700_000_000_000,
        last_processed_candle_ms=1_700_000_000_000,
    )
    session.add(sig)
    await session.commit()

    ordered = [
        Candle(100, 100.2, 100.05, 100, 1_700_000_300_000),
        Candle(100, 100.2, 99, 100, 1_700_000_600_000),
    ]

    class HistMarket:
        async def fetch_live_price(self, symbol):
            return live_quote()

        async def fetch_historical_closed(self, symbol, interval, from_ms, max_bars=500):
            return complete_bundle(closed=ordered, interval="5m")

        async def fetch_kline_bundle(self, symbol, interval, limit=200):
            return complete_bundle(closed=ordered, interval="5m")

        def is_quote_stale(self, q):
            return False

    runtime = RuntimeSettings(id=1, max_pending_minutes=90, max_missed_entry_pct=0.3, holding_time_hours=6, min_net_rr=2)
    monitor = SignalMonitorService(HistMarket(), SignalLifecycleService())
    await monitor.monitor_open_signals(session, runtime)
    await session.refresh(sig)
    assert sig.status == SignalStatus.ACTIVE.value
    assert sig.last_processed_candle_ms == 1_700_000_600_000


@pytest.mark.asyncio
async def test_outbox_not_sent_on_silent_notify(session):
    sig = make_signal()
    session.add(sig)
    await session.commit()
    outbox = NotificationOutboxService()
    await outbox.enqueue(session, f"ACTIVE:{sig.public_id}", "ACTIVE", sig)
    await session.commit()
    with patch.object(type(outbox.notifier), "enabled", new_callable=PropertyMock, return_value=True):
        with patch.object(outbox.notifier, "notify_entry_activated", AsyncMock(return_value=None)):
            assert await outbox.flush_pending(session) == 0
    row = (await session.execute(select(NotificationOutbox))).scalar_one()
    assert row.status == "pending"


@pytest.mark.asyncio
async def test_outbox_http_400_stays_pending(session):
    sig = make_signal()
    session.add(sig)
    await session.commit()
    outbox = NotificationOutboxService()
    await outbox.enqueue(session, f"NEW:{sig.public_id}", "NEW", sig)
    await session.commit()

    class Resp400:
        status_code = 400

        @staticmethod
        def json():
            return {"ok": False}

    with patch.object(type(outbox.notifier), "enabled", new_callable=PropertyMock, return_value=True):
        with patch("httpx.AsyncClient.post", AsyncMock(return_value=Resp400())):
            assert await outbox.flush_pending(session) == 0


@pytest.mark.asyncio
async def test_outbox_disabled_telegram_stays_pending(session):
    sig = make_signal()
    session.add(sig)
    await session.commit()
    outbox = NotificationOutboxService(TelegramNotifier())
    await outbox.enqueue(session, f"EXPIRED:{sig.public_id}", "EXPIRED", sig)
    await session.commit()
    assert await outbox.flush_pending(session) == 0


def test_worker_does_not_poll_telegram():
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "app" / "worker.py"
    text = src.read_text()
    assert "poll_once" not in text
