"""Realistic WAIT → READY → PENDING → fill via SimPositionMonitorService."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.domain.candle_utils import candle_close_ms
from app.domain.candles import Candle
from app.domain.setup_lifecycle import PENDING_ENTRY, POSITION_OPEN
from app.domain.setup_monitor_boundary import first_eligible_bar_open_ms
from app.models import AiProposedSetup, SimPosition, SimPositionSettings
from app.services.market_data import KlineBundle
from app.services.opportunity_monitor import OpportunityMonitorService
from app.services.position import wallet_service as wallet
from app.services.position.position_monitor import SimPositionMonitorService
from tests.test_ai_trader_persistence_contract import _production_conditional_wait_long

INTERVAL_5M = 300_000
INTERVAL_1M = 60_000


def _5m(open_ms: int, *, o: float, h: float, l: float, c: float) -> Candle:
    return Candle(time_ms=open_ms, open=o, high=h, low=l, close=c, volume=1.0)


def _1m(open_ms: int, *, o: float, h: float, l: float, c: float) -> Candle:
    return Candle(time_ms=open_ms, open=o, high=h, low=l, close=c, volume=1.0)


def _sweep_mss_poi_long_bars(boundary_ms: int) -> list[Candle]:
    bars: list[Candle] = []
    for i in range(14, 0, -1):
        open_ms = boundary_ms - i * INTERVAL_5M
        bars.append(_5m(open_ms, o=100.0, h=100.3, l=99.0, c=100.1))
    bars.append(_5m(boundary_ms, o=100.0, h=100.2, l=98.6, c=99.4))
    bars.append(_5m(boundary_ms + INTERVAL_5M, o=99.4, h=100.8, l=99.2, c=100.7))
    bars.append(_5m(boundary_ms + 2 * INTERVAL_5M, o=100.5, h=100.6, l=99.8, c=100.2))
    return bars


@pytest.mark.asyncio
async def test_realistic_conditional_pipeline_monitor_and_sim_fill():
    await init_db()
    created = datetime(2026, 10, 10, 12, 7, tzinfo=timezone.utc)
    boundary = first_eligible_bar_open_ms(created, "5m")
    closed_5m = _sweep_mss_poi_long_bars(boundary)
    last_bar = closed_5m[-1]
    now_ms = candle_close_ms(last_bar, "5m") + 15_000
    now_dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)

    async with SessionLocal() as session:
        await wallet.append_ledger(session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=50_000.0)
        settings = (await session.execute(select(SimPositionSettings))).scalar_one_or_none()
        if not settings:
            session.add(SimPositionSettings(id=1))
        settings = (await session.execute(select(SimPositionSettings))).scalar_one()
        settings.initial_balance_set = True
        settings.auto_open_paper_positions = True
        settings.max_risk_pct_per_position = 100.0
        settings.default_margin_usdt = 50.0
        settings.default_leverage = 10

        from app.services.ai_setup_memory import create_setup_from_structured

        structured = _production_conditional_wait_long()
        row, meta = await create_setup_from_structured(
            session, run_id=100, symbol="BTCUSDT", structured=structured
        )
        assert meta["created"] is True
        row.created_at = created
        row.monitor_from_ms = boundary
        row.monitor_cursor_ms = boundary - 1
        await session.commit()
        setup_id = row.setup_id

    bundle_5m = KlineBundle(
        closed=closed_5m,
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )

    async with SessionLocal() as session:
        mon = OpportunityMonitorService()
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle_5m)):
            with patch("app.services.opportunity_monitor._utc_now", return_value=now_dt):
                summary = await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.commit()
        assert summary["promoted"] == 1
        assert summary["orders"] == 1
        setup = (
            await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == setup_id))
        ).scalar_one()
        assert setup.lifecycle_state == PENDING_ENTRY
        completed = (setup.confirmation_state or {}).get("completed") or []
        assert completed == ["liquidity_sweep", "mss", "entry_zone_touch"]

        pos = (
            await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == setup_id))
        ).scalar_one()
        entry = pos.limit_entry_price or setup.entry_price
        assert entry is not None

        fill_open_ms = now_ms - 120_000
        pre = _1m(fill_open_ms - INTERVAL_1M, o=entry, h=entry + 0.5, l=entry - 0.2, c=entry)
        hit = _1m(fill_open_ms, o=entry, h=entry + 0.5, l=entry - 0.5, c=entry)
        bundle_1m = KlineBundle(
            closed=[pre, hit],
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="1m",
        )
        pos.monitor_from_ms = pre.time_ms
        pos.last_processed_candle_ms = pre.time_ms
        await session.commit()

        sim = SimPositionMonitorService()
        with patch.object(sim.market, "fetch_kline_bundle", AsyncMock(return_value=bundle_1m)):
            processed = await sim.monitor_session(session)
        await session.commit()
        assert processed >= 1
        pos = (
            await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == setup_id))
        ).scalar_one()
        assert pos.status == "open"
        setup = (
            await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == setup_id))
        ).scalar_one()
        assert setup.lifecycle_state == POSITION_OPEN
