"""Chronology and 5m/1m data-quality edge regressions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.domain.candle_utils import candle_close_ms
from app.domain.candles import Candle
from app.domain.setup_lifecycle import PENDING_ENTRY
from app.models import AiProposedSetup, SimPosition, SimPositionSettings
from app.services.market_data import KlineBundle
from app.services.opportunity_monitor import OpportunityMonitorService
from app.services.position.position_monitor import SimPositionMonitorService

INTERVAL_5M = 300_000
INTERVAL_1M = 60_000


def _1m(open_ms: int, *, low: float, high: float, close: float) -> Candle:
    return Candle(time_ms=open_ms, open=close, high=high, low=low, close=close, volume=1.0)


@pytest.mark.asyncio
async def test_sim_suspends_fill_on_stale_complete_5m_bundle():
    await init_db()
    order_ms = 1_700_000_000_000
    monitor_at = datetime.fromtimestamp((order_ms + INTERVAL_1M * 3) / 1000, tz=timezone.utc)
    stale_5m = Candle(time_ms=order_ms - INTERVAL_5M * 10, open=100, high=101, low=99, close=100.5, volume=1)
    fill_1m = _1m(order_ms + INTERVAL_1M, low=99.0, high=100.5, close=100.2)

    async with SessionLocal() as session:
        session.add(
            AiProposedSetup(
                setup_id="stale-5m-setup",
                run_id=1,
                symbol="BTCUSDT",
                direction="LONG",
                status="open",
                lifecycle_state=PENDING_ENTRY,
                monitoring_enabled=True,
                monitor_cursor_ms=order_ms - INTERVAL_5M,
                entry_price=100.0,
                stop_loss=94.0,
                take_profit=115.0,
                invalidation_level=93.0,
                expires_at=monitor_at + timedelta(hours=12),
            )
        )
        session.add(
            SimPosition(
                public_id="stale-5m-pos",
                symbol="BTCUSDT",
                direction="LONG",
                status="pending_entry",
                order_type="limit",
                margin_usdt=50.0,
                leverage=10,
                limit_entry_price=100.0,
                stop_loss=94.0,
                take_profit=115.0,
                ai_setup_id="stale-5m-setup",
                monitor_from_ms=order_ms,
                last_processed_candle_ms=order_ms,
                extra={"auto_paper": True},
            )
        )
        await session.commit()

    sim = SimPositionMonitorService()
    bundle_1m = KlineBundle(
        closed=[fill_1m],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="1m",
    )
    bundle_5m = KlineBundle(
        closed=[stale_5m],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )

    async with SessionLocal() as session:
        with patch("app.services.opportunity_monitor._utc_now", return_value=monitor_at):
            with patch("app.services.paper_auto_trading._utc_now", return_value=monitor_at):
                with patch.object(sim.market, "fetch_kline_bundle", AsyncMock(side_effect=lambda s, i, l: bundle_5m if i == "5m" else bundle_1m)):
                    await sim.monitor_session(session)
        await session.commit()
        setup = (
            await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "stale-5m-setup"))
        ).scalar_one()
        pos = (
            await session.execute(select(SimPosition).where(SimPosition.public_id == "stale-5m-pos"))
        ).scalar_one()
        assert pos.status == "pending_entry"
        assert (setup.objective_notes or {}).get("pending_fill_block_reason") == "stale_5m_market_data"


@pytest.mark.asyncio
async def test_opportunity_does_not_advance_5m_cursor_on_reconcile_fill_hint():
    await init_db()
    order_ms = 1_800_000_000_000
    cursor_before = order_ms - INTERVAL_5M
    fill_1m = _1m(order_ms + INTERVAL_1M, low=99.0, high=100.5, close=100.2)
    inv_5m = Candle(time_ms=order_ms + INTERVAL_5M, open=95, high=96, low=91, close=92, volume=1)
    monitor_at = datetime.fromtimestamp((candle_close_ms(inv_5m, "5m") + 5_000) / 1000, tz=timezone.utc)

    async with SessionLocal() as session:
        setup = AiProposedSetup(
            setup_id="cursor-hold",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=PENDING_ENTRY,
            monitoring_enabled=True,
            monitor_from_ms=order_ms - INTERVAL_5M,
            monitor_cursor_ms=cursor_before,
            entry_price=100.0,
            stop_loss=94.0,
            take_profit=115.0,
            invalidation_level=93.0,
            expires_at=monitor_at + timedelta(hours=12),
        )
        session.add(setup)
        session.add(
            SimPosition(
                public_id="cursor-hold-pos",
                symbol="BTCUSDT",
                direction="LONG",
                status="pending_entry",
                order_type="limit",
                margin_usdt=50.0,
                leverage=10,
                limit_entry_price=100.0,
                stop_loss=94.0,
                take_profit=115.0,
                ai_setup_id="cursor-hold",
                monitor_from_ms=order_ms,
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
        closed=[_1m(order_ms + 60_000, low=98, high=99, close=98.5), fill_1m],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="1m",
    )
    mon = OpportunityMonitorService()
    async with SessionLocal() as session:
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(side_effect=lambda s, i, l: bundle_5m if i == "5m" else bundle_1m)):
            with patch("app.services.opportunity_monitor._utc_now", return_value=monitor_at):
                with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 5, 600)):
                    await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.commit()
        row = (
            await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "cursor-hold"))
        ).scalar_one()
        assert row.monitor_cursor_ms == cursor_before
