"""Worker cycle: 5m structural invalidation must run before 1m limit fills."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.domain.candle_utils import candle_close_ms
from app.domain.candles import Candle
from app.domain.setup_lifecycle import INVALIDATED, PENDING_ENTRY, POSITION_OPEN
from app.models import AiProposedSetup, SimPosition, SimPositionSettings
from app.services.market_data import KlineBundle
from app.services.position import wallet_service as wallet
from app.worker import BotWorker

INTERVAL_5M = 300_000
INTERVAL_1M = 60_000


def _5m(open_ms: int, *, o: float, h: float, l: float, c: float) -> Candle:
    return Candle(time_ms=open_ms, open=o, high=h, low=l, close=c, volume=1.0)


def _1m(open_ms: int, *, o: float, h: float, l: float, c: float) -> Candle:
    return Candle(time_ms=open_ms, open=o, high=h, low=l, close=c, volume=1.0)


@pytest.mark.asyncio
async def test_worker_invalidates_pending_on_5m_before_1m_limit_fill():
    await init_db()
    base = datetime(2026, 10, 10, 14, 0, tzinfo=timezone.utc)
    base_ms = int(base.timestamp() * 1000)
    five_m_open = base_ms
    inv_5m = _5m(five_m_open, o=95.0, h=96.0, l=91.0, c=92.0)
    order_ms = five_m_open - INTERVAL_1M
    fill_1m_open = five_m_open + INTERVAL_5M + 120_000
    fill_1m = _1m(fill_1m_open, o=99.5, h=100.5, l=99.0, c=100.2)

    cycle_now = datetime.fromtimestamp((fill_1m_open + INTERVAL_1M + 5_000) / 1000, tz=timezone.utc)

    async with SessionLocal() as session:
        await wallet.append_ledger(session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=50_000.0)
        settings = (await session.execute(select(SimPositionSettings))).scalar_one_or_none()
        if not settings:
            session.add(SimPositionSettings(id=1))
        settings = (await session.execute(select(SimPositionSettings))).scalar_one()
        settings.initial_balance_set = True
        settings.auto_open_paper_positions = True

        setup = AiProposedSetup(
            setup_id="worker-inv-order",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=PENDING_ENTRY,
            monitoring_enabled=True,
            monitor_from_ms=five_m_open,
            monitor_cursor_ms=five_m_open - INTERVAL_5M,
            entry_price=100.0,
            stop_loss=94.0,
            take_profit=115.0,
            invalidation_level=93.0,
            expires_at=cycle_now + timedelta(hours=12),
            last_validation_result={"ok": True},
        )
        session.add(setup)
        session.add(
            SimPosition(
                public_id="pos-worker-inv",
                symbol="BTCUSDT",
                direction="LONG",
                status="pending_entry",
                order_type="limit",
                margin_usdt=50.0,
                leverage=10,
                limit_entry_price=100.0,
                stop_loss=94.0,
                take_profit=115.0,
                ai_setup_id="worker-inv-order",
                monitor_from_ms=order_ms,
                last_processed_candle_ms=order_ms,
                extra={"auto_paper": True},
            )
        )
        await session.commit()

    bundle_5m = KlineBundle(
        closed=[inv_5m],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )
    bundle_1m = KlineBundle(
        closed=[fill_1m],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="1m",
    )

    worker = BotWorker()

    async def _fetch(symbol: str, interval: str, limit: int):
        if interval == "5m":
            return bundle_5m
        if interval == "1m":
            return bundle_1m
        return KlineBundle(
            closed=[],
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=False,
            interval=interval,
        )

    with patch("app.worker.try_acquire_worker_lock", AsyncMock(return_value=True)):
        with patch("app.worker.release_worker_lock", AsyncMock()):
            with patch.object(worker, "monitor") as mock_sig_mon:
                mock_sig_mon.monitor_open_signals = AsyncMock(return_value={})
                with patch("app.services.opportunity_monitor._utc_now", return_value=cycle_now):
                    with patch("app.services.paper_auto_trading._utc_now", return_value=cycle_now):
                        with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 5, 600)):
                            with patch(
                                "app.services.market_data.MarketDataService.fetch_kline_bundle",
                                AsyncMock(side_effect=_fetch),
                            ):
                                await worker.run_cycle()

    async with SessionLocal() as session:
        setup = (
            await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "worker-inv-order"))
        ).scalar_one()
        pos = (
            await session.execute(select(SimPosition).where(SimPosition.public_id == "pos-worker-inv"))
        ).scalar_one()
        assert setup.lifecycle_state == INVALIDATED
        assert pos.status == "cancelled"
        assert pos.status != "open"
        summary = await wallet.wallet_summary(session)
        assert summary["margin_in_use"] == 0.0


@pytest.mark.asyncio
async def test_worker_fills_on_earlier_1m_before_later_5m_invalidation():
    """Event-time: 1m limit touch closes before 5m invalidation — must enter, not cancel."""
    await init_db()
    base = datetime(2026, 10, 10, 16, 0, tzinfo=timezone.utc)
    order_ms = int(base.timestamp() * 1000)
    fill_1m = _1m(order_ms + INTERVAL_1M, o=99.5, h=100.5, l=99.0, c=100.2)
    inv_5m = _5m(order_ms + INTERVAL_5M, o=95.0, h=96.0, l=91.0, c=92.0)
    cycle_now = datetime.fromtimestamp((candle_close_ms(inv_5m, "5m") + 5_000) / 1000, tz=timezone.utc)

    async with SessionLocal() as session:
        await wallet.append_ledger(session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=50_000.0)
        settings = (await session.execute(select(SimPositionSettings))).scalar_one_or_none()
        if not settings:
            session.add(SimPositionSettings(id=1))
        settings = (await session.execute(select(SimPositionSettings))).scalar_one()
        settings.initial_balance_set = True
        settings.auto_open_paper_positions = True

        setup = AiProposedSetup(
            setup_id="worker-fill-first",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=PENDING_ENTRY,
            monitoring_enabled=True,
            monitor_from_ms=order_ms - INTERVAL_5M,
            monitor_cursor_ms=order_ms - INTERVAL_5M,
            entry_price=100.0,
            stop_loss=94.0,
            take_profit=115.0,
            invalidation_level=93.0,
            expires_at=cycle_now + timedelta(hours=12),
            last_validation_result={"ok": True},
        )
        session.add(setup)
        session.add(
            SimPosition(
                public_id="pos-fill-first",
                symbol="BTCUSDT",
                direction="LONG",
                status="pending_entry",
                order_type="limit",
                margin_usdt=50.0,
                leverage=10,
                limit_entry_price=100.0,
                stop_loss=94.0,
                take_profit=115.0,
                ai_setup_id="worker-fill-first",
                monitor_from_ms=order_ms,
                last_processed_candle_ms=order_ms,
                extra={"auto_paper": True},
            )
        )
        await session.commit()

    bundle_5m = KlineBundle(
        closed=[inv_5m],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )
    bundle_1m = KlineBundle(
        closed=[fill_1m],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="1m",
    )

    worker = BotWorker()

    async def _fetch(symbol: str, interval: str, limit: int):
        if interval == "5m":
            return bundle_5m
        if interval == "1m":
            return bundle_1m
        return bundle_5m

    with patch("app.worker.try_acquire_worker_lock", AsyncMock(return_value=True)):
        with patch("app.worker.release_worker_lock", AsyncMock()):
            with patch.object(worker, "monitor") as mock_sig_mon:
                mock_sig_mon.monitor_open_signals = AsyncMock(return_value={})
                with patch("app.services.opportunity_monitor._utc_now", return_value=cycle_now):
                    with patch("app.services.paper_auto_trading._utc_now", return_value=cycle_now):
                        with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 5, 600)):
                            with patch(
                                "app.services.market_data.MarketDataService.fetch_kline_bundle",
                                AsyncMock(side_effect=_fetch),
                            ):
                                await worker.run_cycle()

    async with SessionLocal() as session:
        setup = (
            await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "worker-fill-first"))
        ).scalar_one()
        pos = (
            await session.execute(select(SimPosition).where(SimPosition.public_id == "pos-fill-first"))
        ).scalar_one()
        assert pos.status == "open"
        assert setup.lifecycle_state == POSITION_OPEN
