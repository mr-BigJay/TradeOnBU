"""Full BALANCED integration: 1m boundary, integrity order gate, confirm → order → fill."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.domain.candles import Candle
from app.domain.candle_utils import candle_close_ms
from app.domain.entry_execution import ENTRY_MODE_BALANCED, ENTRY_MODE_STRICT
from app.domain.setup_lifecycle import PENDING_ENTRY, POSITION_OPEN, READY_LONG, WAIT_LONG
from app.domain.setup_monitor_boundary import first_eligible_bar_open_ms, monitor_start_open_ms
from app.models import AiProposedSetup, SimPosition
from app.services.conditional_setup import apply_conditional_plan_to_setup_row
from app.services.market_data import KlineBundle
from app.services.opportunity_monitor import OpportunityMonitorService
from app.services.position.position_monitor import SimPositionMonitorService
from tests.test_confirmation_replay_chronology import STEP1, STEP5, ZONE_HI, ZONE_LO, _build_positive_same_5m


def test_balanced_monitor_boundary_first_minute_of_5m_candle():
    closed_5m, _, bar_5m, _ = _build_positive_same_5m()
    t5 = bar_5m.time_ms
    created = datetime.fromtimestamp((t5 + 30_000) / 1000, tz=timezone.utc)
    strict_boundary = first_eligible_bar_open_ms(created, "5m")
    balanced_boundary = monitor_start_open_ms(created, ENTRY_MODE_BALANCED)
    assert strict_boundary == t5 + STEP5
    assert balanced_boundary == t5 + STEP1


def test_apply_conditional_plan_balanced_uses_1m_boundary():
    class Row:
        pass

    row = Row()
    t5 = 100 * STEP5
    created = datetime.fromtimestamp((t5 + 45_000) / 1000, tz=timezone.utc)
    plan = {
        "potential_direction": "LONG",
        "confirmation_sequence": ["entry_zone_touch"],
        "entry_zone_low": ZONE_LO,
        "entry_zone_high": ZONE_HI,
        "proposed_entry_mid": 100.0,
        "proposed_stop_loss": 90.0,
        "proposed_take_profit": 130.0,
        "structural_invalidation_level": 89.0,
    }
    apply_conditional_plan_to_setup_row(
        row,
        plan=plan,
        run_id=1,
        structured={"confirmation_type": "sweep_mss_poi"},
        created_at=created,
        entry_mode=ENTRY_MODE_BALANCED,
    )
    assert row.monitor_from_ms == t5 + STEP1
    apply_conditional_plan_to_setup_row(
        row,
        plan=plan,
        run_id=1,
        structured={"confirmation_type": "sweep_mss_poi"},
        created_at=created,
        entry_mode=ENTRY_MODE_STRICT,
    )
    assert row.monitor_from_ms == t5 + STEP5


def _1m_touch(open_ms: int) -> Candle:
    return Candle(time_ms=open_ms, open=100.0, high=101.0, low=99.5, close=100.2, volume=1.0)


@pytest.mark.asyncio
async def test_balanced_ready_does_not_order_when_1m_integrity_fails():
    await init_db()
    closed_5m, closed_1m, bar_5m, _ = _build_positive_same_5m()
    boundary = bar_5m.time_ms

    async with SessionLocal() as session:
        from app.services.runtime_settings import get_runtime_settings

        rt = await get_runtime_settings(session)
        rt.paper_entry_mode = ENTRY_MODE_BALANCED
        rt.balanced_entry_shadow_mode = False
        await session.commit()

        from tests.test_opportunity_monitor import _wallet_and_settings

        await _wallet_and_settings(session, auto=True)
        setup = AiProposedSetup(
            setup_id="integrity-block-1",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=READY_LONG,
            monitoring_enabled=True,
            monitor_from_ms=boundary,
            monitor_cursor_ms=bar_5m.time_ms - STEP5,
            entry_price=100.0,
            entry_zone_low=ZONE_LO,
            entry_zone_high=ZONE_HI,
            stop_loss=90.0,
            take_profit=130.0,
            invalidation_level=89.0,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            confirmation_state={
                "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "required": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "completed": ["liquidity_sweep", "mss"],
                "missing": ["entry_zone_touch"],
                "confirmation_timestamps": {"liquidity_sweep": boundary, "mss": boundary + STEP1},
                "confirmation_engine": "BALANCED",
                "monitor_1m_cursor_ms": boundary + 2 * STEP1,
            },
            structured_snapshot={"confirmation_type": "sweep_mss_poi"},
        )
        session.add(setup)
        await session.commit()

        now_ms = candle_close_ms(closed_1m[-1], "1m") + 5_000
        bundle_5m = KlineBundle(
            closed=closed_5m,
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="5m",
        )
        bundle_1m = KlineBundle(
            closed=closed_1m,
            forming=None,
            gaps_detected=[],
            gaps_remaining=[boundary + STEP1],
            data_complete=False,
            interval="1m",
        )
        mon = OpportunityMonitorService()

        async def _fetch(symbol: str, interval: str, limit: int):
            return bundle_1m if interval == "1m" else bundle_5m

        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(side_effect=_fetch)):
            with patch("app.services.opportunity_monitor._utc_now") as mock_now:
                mock_now.return_value = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
                with patch(
                    "app.services.opportunity_monitor.is_market_data_fresh",
                    return_value=(True, 30, 330),
                ):
                    with patch(
                        "app.services.opportunity_monitor.setup_history_covers_cursor",
                        return_value=(True, "ok"),
                    ):
                        with patch(
                            "app.services.opportunity_monitor.detect_skipped_closed_bars",
                            return_value=[],
                        ):
                            summary = await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.commit()
        assert summary.get("orders", 0) == 0
        pos = (
            await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == "integrity-block-1"))
        ).scalar_one_or_none()
        assert pos is None
        row = (
            await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "integrity-block-1"))
        ).scalar_one()
        assert row.lifecycle_state == READY_LONG


@pytest.mark.asyncio
async def test_balanced_full_e2e_pre_entry_order_and_post_order_fill_only():
    """BALANCED confirms on 1m, limit before zone touch, ignores pre-order touch, fills after."""
    await init_db()
    closed_5m, closed_1m, bar_5m, _ = _build_positive_same_5m()
    t5 = bar_5m.time_ms
    boundary = bar_5m.time_ms - 14 * STEP5
    m3 = t5 + 3 * STEP1
    order_now_ms = candle_close_ms(Candle(time_ms=m3, open=0, high=0, low=0, close=0, volume=0), "1m") + 2_000
    created = datetime.fromtimestamp((boundary - STEP5) / 1000, tz=timezone.utc)

    async with SessionLocal() as session:
        from app.services.runtime_settings import get_runtime_settings

        rt = await get_runtime_settings(session)
        rt.paper_entry_mode = ENTRY_MODE_BALANCED
        rt.balanced_entry_shadow_mode = False
        await session.commit()

        from tests.test_opportunity_monitor import _wallet_and_settings

        await _wallet_and_settings(session, auto=True)
        setup = AiProposedSetup(
            setup_id="bal-e2e-1",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=WAIT_LONG,
            monitoring_enabled=True,
            monitor_from_ms=boundary,
            monitor_cursor_ms=bar_5m.time_ms - STEP5,
            created_at=created,
            entry_price=100.0,
            entry_zone_low=ZONE_LO,
            entry_zone_high=ZONE_HI,
            stop_loss=90.0,
            take_profit=130.0,
            invalidation_level=89.0,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            confirmation_state={
                "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "required": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "completed": [],
                "missing": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "confirmation_timestamps": {},
                "monitor_1m_cursor_ms": boundary - 1,
            },
            conditional_plan={"confirmation_sequence": ["liquidity_sweep", "mss", "entry_zone_touch"]},
            structured_snapshot={"confirmation_type": "sweep_mss_poi"},
        )
        session.add(setup)
        await session.commit()

        bundle_5m = KlineBundle(
            closed=closed_5m,
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="5m",
        )
        bundle_1m = KlineBundle(
            closed=closed_1m,
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="1m",
        )
        mon = OpportunityMonitorService()

        async def _fetch(symbol: str, interval: str, limit: int):
            return bundle_1m if interval == "1m" else bundle_5m

        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(side_effect=_fetch)):
            with patch("app.services.opportunity_monitor._utc_now") as mock_now:
                mock_now.return_value = datetime.fromtimestamp(order_now_ms / 1000, tz=timezone.utc)
                with patch(
                    "app.services.opportunity_monitor.is_market_data_fresh",
                    return_value=(True, 30, 330),
                ):
                    with patch(
                        "app.services.opportunity_monitor.setup_history_covers_cursor",
                        return_value=(True, "ok"),
                    ):
                        with patch(
                            "app.services.opportunity_monitor.detect_skipped_closed_bars",
                            return_value=[],
                        ):
                            import datetime as real_datetime

                            order_wall = datetime.fromtimestamp(order_now_ms / 1000, tz=timezone.utc)
                            with patch("app.services.position.position_service.datetime") as pos_dt:
                                pos_dt.now = lambda tz=None: order_wall
                                pos_dt.side_effect = lambda *args, **kwargs: real_datetime.datetime(*args, **kwargs)
                                summary = await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.commit()

        row = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "bal-e2e-1"))).scalar_one()
        assert summary.get("orders", 0) >= 1
        assert row.lifecycle_state == PENDING_ENTRY
        assert order_now_ms < candle_close_ms(bar_5m, "5m")
        ts = (row.confirmation_state or {}).get("confirmation_timestamps") or {}
        assert "liquidity_sweep" in ts and "mss" in ts
        assert "entry_zone_touch" not in ts

        pos = (await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == "bal-e2e-1"))).scalar_one()
        assert pos.status == "pending_entry"
        order_from_ms = pos.monitor_from_ms or int(order_wall.timestamp() * 1000)

        pre_touch = _1m_touch(order_from_ms - STEP1)
        post_touch = _1m_touch(order_from_ms + STEP1)
        sim = SimPositionMonitorService()
        fresh_5m = bundle_5m

        def _sim_pre(_symbol: str, interval: str, _limit: int):
            return fresh_5m if interval == "5m" else KlineBundle(
                closed=[pre_touch],
                forming=None,
                gaps_detected=[],
                gaps_remaining=[],
                data_complete=True,
                interval="1m",
            )

        with patch.object(sim.market, "fetch_kline_bundle", AsyncMock(side_effect=_sim_pre)):
            with patch("app.services.opportunity_monitor._utc_now", return_value=order_wall):
                with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 5, 600)):
                    await sim.monitor_session(session)
        await session.commit()
        pos = (await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == "bal-e2e-1"))).scalar_one()
        assert pos.status == "pending_entry"

        def _sim_post(_symbol: str, interval: str, _limit: int):
            return fresh_5m if interval == "5m" else KlineBundle(
                closed=[post_touch],
                forming=None,
                gaps_detected=[],
                gaps_remaining=[],
                data_complete=True,
                interval="1m",
            )

        with patch.object(sim.market, "fetch_kline_bundle", AsyncMock(side_effect=_sim_post)):
            with patch("app.services.opportunity_monitor._utc_now", return_value=order_wall):
                with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 5, 600)):
                    await sim.monitor_session(session)
        await session.commit()
        pos = (await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == "bal-e2e-1"))).scalar_one()
        assert pos.status == "open"
        row = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "bal-e2e-1"))).scalar_one()
        assert row.lifecycle_state == POSITION_OPEN


@pytest.mark.asyncio
async def test_dry_run_blocks_maybe_auto_open_even_with_entry_mode_passed():
    await init_db()
    async with SessionLocal() as session:
        from app.domain.entry_execution import ENTRY_MODE_STRICT
        from app.services.runtime_settings import get_runtime_settings

        rt = await get_runtime_settings(session)
        rt.paper_entry_dry_run = True
        await session.commit()

        from tests.test_opportunity_monitor import _wallet_and_settings

        await _wallet_and_settings(session, auto=True)
        setup = AiProposedSetup(
            setup_id="dry-1",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=READY_LONG,
            entry_price=100.0,
            entry_zone_low=99.0,
            entry_zone_high=101.0,
            stop_loss=90.0,
            take_profit=130.0,
            structured_snapshot={"decision": "LONG"},
        )
        session.add(setup)
        await session.commit()

        from app.services.paper_auto_trading import maybe_auto_open_paper_position

        meta = await maybe_auto_open_paper_position(
            session,
            run_id=1,
            setup=setup,
            validation={"ai_decision": "LONG", "acceptable_rr": True, "objective_ready": True},
            structured={"decision": "LONG"},
            symbol="BTCUSDT",
            require_ai_flags=False,
            entry_mode=ENTRY_MODE_STRICT,
        )
        assert meta["skipped"] is True
        assert meta["reason"] == "paper_entry_dry_run"
