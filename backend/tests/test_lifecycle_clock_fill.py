"""Clock-controlled fill: no pre-order 1m bar can fill; post-order bar can."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.domain.candles import Candle
from app.domain.setup_lifecycle import PENDING_ENTRY, POSITION_OPEN
from app.models import AiProposedSetup, SimPosition, SimPositionSettings
from app.services.market_data import KlineBundle
from app.services.opportunity_monitor import OpportunityMonitorService
from app.services.position import wallet_service as wallet
from app.services.position.position_monitor import SimPositionMonitorService
from tests.test_ai_trader_persistence_contract import _production_conditional_wait_long

T_ORDER = datetime(2026, 10, 10, 14, 0, 0, tzinfo=timezone.utc)
T_ORDER_MS = int(T_ORDER.timestamp() * 1000)


def _1m(ms: int, lo: float, hi: float) -> Candle:
    mid = (lo + hi) / 2
    return Candle(time_ms=ms, open=mid, high=hi, low=lo, close=mid, volume=1.0)


@pytest.mark.asyncio
async def test_limit_fill_only_after_order_creation_time():
    await init_db()
    from app.domain.setup_monitor_boundary import first_eligible_bar_open_ms
    from app.services.ai_setup_memory import create_setup_from_structured

    created = T_ORDER - __import__("datetime").timedelta(minutes=30)
    boundary = first_eligible_bar_open_ms(created, "5m")

    async with SessionLocal() as session:
        await wallet.append_ledger(session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=50_000.0)
        settings = (await session.execute(select(SimPositionSettings))).scalar_one_or_none()
        if not settings:
            session.add(SimPositionSettings(id=1))
        settings = (await session.execute(select(SimPositionSettings))).scalar_one()
        settings.initial_balance_set = True
        settings.auto_open_paper_positions = True
        settings.max_risk_pct_per_position = 100.0

        row, meta = await create_setup_from_structured(
            session, run_id=1, symbol="BTCUSDT", structured=_production_conditional_wait_long()
        )
        assert meta["created"]
        row.created_at = created
        row.monitor_from_ms = boundary
        row.monitor_cursor_ms = boundary - 1
        row.confirmation_state = {
            "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
            "required": ["liquidity_sweep", "mss", "entry_zone_touch"],
            "completed": ["liquidity_sweep", "mss", "entry_zone_touch"],
            "missing": [],
            "confirmation_timestamps": {
                "liquidity_sweep": boundary,
                "mss": boundary + 300_000,
                "entry_zone_touch": boundary + 600_000,
            },
        }
        row.lifecycle_state = "READY_LONG"
        row.structured_snapshot = {
            **_production_conditional_wait_long(),
            "liquidity_event_clear": True,
            "poi_reaction_clear": True,
            "structural_confirmation_clear": True,
            "logical_invalidation_clear": True,
        }
        await session.commit()
        setup_id = row.setup_id

    from app.domain.candle_utils import candle_close_ms

    last_5m = boundary + 600_000
    promote_at = datetime.fromtimestamp((candle_close_ms(Candle(time_ms=last_5m, open=0, high=0, low=0, close=0, volume=0), "5m") + 5000) / 1000, tz=timezone.utc)

    import datetime as real_datetime

    async with SessionLocal() as session:
        with patch("app.services.opportunity_monitor._utc_now", return_value=promote_at):
            with patch("app.services.position.position_service.datetime") as pos_dt:
                pos_dt.now = lambda tz=None: T_ORDER
                pos_dt.side_effect = lambda *args, **kwargs: real_datetime.datetime(*args, **kwargs)
                mon = OpportunityMonitorService()
                closed_5m = [
                    Candle(time_ms=boundary + i * 300_000, open=100, high=101, low=99, close=100.2, volume=1)
                    for i in range(3)
                ]
                bundle5 = KlineBundle(
                    closed=closed_5m,
                    forming=None,
                    gaps_detected=[],
                    gaps_remaining=[],
                    data_complete=True,
                    interval="5m",
                )
                with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle5)):
                    await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.commit()
        pos = (await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == setup_id))).scalar_one()
        assert pos.status == "pending_entry"
        order_from_ms = pos.monitor_from_ms
        assert order_from_ms >= T_ORDER_MS - 1000

        pre_bar = _1m(order_from_ms - 60_000, 99.0, 101.0)
        post_bar = _1m(order_from_ms + 60_000, 99.0, 101.0)
        bundle_pre = KlineBundle(
            closed=[pre_bar],
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="1m",
        )
        sim = SimPositionMonitorService()
        fresh_5m = KlineBundle(
            closed=closed_5m,
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="5m",
        )

        def _sim_fetch(_symbol: str, interval: str, _limit: int):
            return fresh_5m if interval == "5m" else bundle_pre

        with patch.object(sim.market, "fetch_kline_bundle", AsyncMock(side_effect=_sim_fetch)):
            with patch("app.services.opportunity_monitor._utc_now", return_value=promote_at):
                with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 5, 600)):
                    await sim.monitor_session(session)
        await session.commit()
        pos = (await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == setup_id))).scalar_one()
        assert pos.status == "pending_entry"

        bundle_post = KlineBundle(
            closed=[post_bar],
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="1m",
        )
        def _sim_fetch_post(_symbol: str, interval: str, _limit: int):
            return fresh_5m if interval == "5m" else bundle_post

        with patch.object(sim.market, "fetch_kline_bundle", AsyncMock(side_effect=_sim_fetch_post)):
            with patch("app.services.opportunity_monitor._utc_now", return_value=promote_at):
                with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 5, 600)):
                    await sim.monitor_session(session)
        await session.commit()
        pos = (await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == setup_id))).scalar_one()
        assert pos.status == "open"
        setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == setup_id))).scalar_one()
        assert setup.lifecycle_state == POSITION_OPEN
