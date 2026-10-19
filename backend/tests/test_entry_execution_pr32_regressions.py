"""PR #32 pre-merge regressions: shadow, 1m integrity, mode switch, gap, no retro fill, STRICT."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.domain.candles import Candle
from app.domain.candle_utils import candle_close_ms
from app.domain.entry_execution import ENTRY_MODE_BALANCED, ENTRY_MODE_STRICT
from app.domain.setup_lifecycle import PENDING_ENTRY, READY_LONG, WAIT_LONG
from app.models import AiProposedSetup, SimPosition
from app.services.confirmation_policy import resolve_confirmation_policy
from app.services.entry_execution_mode_switch import reconcile_setup_entry_engine
from app.services.ict_confirmation_engine import process_bar_sequential
from app.services.market_data import KlineBundle
from app.services.monitor_1m_integrity import assess_1m_monitor_integrity
from app.services.opportunity_monitor import OpportunityMonitorService
from app.services.position.position_monitor import SimPositionMonitorService
from tests.test_confirmation_replay_chronology import STEP1, ZONE_HI, ZONE_LO, _build_positive_same_5m


def test_1m_integrity_rejects_gaps_and_incomplete():
    bars = [
        Candle(time_ms=1_000_000, open=1, high=2, low=1, close=1.5, volume=1),
        Candle(time_ms=1_120_000, open=1.5, high=2, low=1, close=1.6, volume=1),
    ]
    ok, reason, _ = assess_1m_monitor_integrity(
        closed_1m=bars,
        data_complete=False,
        gaps_remaining=[1_060_000],
        boundary_ms=900_000,
        cursor_ms=990_000,
        now_ms=1_200_000,
    )
    assert not ok
    assert reason in ("1m_bundle_incomplete", "1m_gaps_remaining", "1m_discontinuity_in_new_range")


def test_mode_switch_balanced_to_strict_strips_1m_stamps():
    class Row:
        pass

    row = Row()
    row.monitor_from_ms = 1_000_000
    row.objective_notes = {}
    row.confirmation_state = {
        "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
        "confirmation_timestamps": {
            "liquidity_sweep": 1_000_000,
            "mss": 1_060_000,
        },
        "confirmation_evidence": {
            "liquidity_sweep": {"engine": "BALANCED", "granularity": "1m"},
            "mss": {"engine": "BALANCED", "granularity": "1m"},
        },
        "confirmation_engine": "BALANCED",
    }

    reconcile_setup_entry_engine(row, live_engine=ENTRY_MODE_STRICT)
    conf = row.confirmation_state
    assert conf["confirmation_engine"] == ENTRY_MODE_STRICT
    assert "mss" not in conf.get("confirmation_timestamps", {})
    assert "liquidity_sweep" not in conf.get("confirmation_timestamps", {})


def test_strict_promotion_ignores_optional_confirmations_in_ai_snapshot():
    seq = ["liquidity_sweep", "mss", "entry_zone_touch"]
    policy = resolve_confirmation_policy(
        sequence=seq,
        structured={
            "confirmation_type": "sweep_mss_poi",
            "optional_confirmations": ["liquidity_sweep", "mss", "entry_zone_touch"],
        },
    )
    assert policy.required == ("liquidity_sweep", "mss", "entry_zone_touch")
    assert policy.optional == ()


def test_strict_process_bar_unchanged_with_optional_in_snapshot():
    seq = ["entry_zone_touch"]
    policy = resolve_confirmation_policy(
        sequence=seq,
        structured={"optional_confirmations": ["entry_zone_touch"]},
    )
    bar = Candle(time_ms=300_000, open=100, high=101, low=99.5, close=100.2, volume=1)
    progress = process_bar_sequential(
        direction="LONG",
        sequence=list(policy.sequence),
        confirmation_state={"confirmation_timestamps": {}, "confirmation_evidence": {}},
        bar=bar,
        window=[bar],
        zone_lo=99.0,
        zone_hi=101.0,
        invalidation_level=90.0,
        structured={},
        monitor_from_ms=0,
    )
    assert progress.completed == ["entry_zone_touch"]


@pytest.mark.asyncio
async def test_shadow_does_not_block_strict_paper_order():
    await init_db()
    async with SessionLocal() as session:
        from app.services.runtime_settings import get_runtime_settings

        rt = await get_runtime_settings(session)
        rt.paper_entry_mode = ENTRY_MODE_BALANCED
        rt.balanced_entry_shadow_mode = True
        rt.paper_entry_dry_run = False
        await session.commit()

        from tests.test_opportunity_monitor import _wallet_and_settings

        await _wallet_and_settings(session, auto=True)
        setup = AiProposedSetup(
            setup_id="shadow-order-1",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=READY_LONG,
            monitoring_enabled=True,
            monitor_from_ms=0,
            monitor_cursor_ms=300_000,
            entry_price=100.0,
            entry_zone_low=99.0,
            entry_zone_high=101.0,
            stop_loss=90.0,
            take_profit=130.0,
            invalidation_level=89.0,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            confirmation_state={
                "sequence": ["entry_zone_touch"],
                "required": ["entry_zone_touch"],
                "completed": ["entry_zone_touch"],
                "missing": [],
                "confirmation_timestamps": {"entry_zone_touch": 300_000},
                "confirmation_evidence": {
                    "entry_zone_touch": {
                        "bar_low": 99.5,
                        "bar_high": 100.5,
                        "zone_lo": 99.0,
                        "zone_hi": 101.0,
                        "engine": "STRICT",
                        "granularity": "5m",
                    }
                },
                "confirmation_engine": "STRICT",
            },
            structured_snapshot={"confirmation_type": "sweep_mss_poi"},
        )
        session.add(setup)
        await session.commit()

        bar = Candle(time_ms=300_000, open=100, high=101, low=99.5, close=100.2, volume=1)
        bundle = KlineBundle(
            closed=[bar],
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="5m",
        )
        bundle_1m = KlineBundle(closed=[], forming=None, gaps_detected=[], gaps_remaining=[], data_complete=True, interval="1m")
        mon = OpportunityMonitorService()

        async def _fetch(symbol: str, interval: str, limit: int):
            return bundle_1m if interval == "1m" else bundle

        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(side_effect=_fetch)):
            with patch("app.services.opportunity_monitor._utc_now") as mock_now:
                mock_now.return_value = datetime.fromtimestamp(600_000 / 1000, tz=timezone.utc)
                with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 30, 330)):
                    with patch(
                        "app.services.opportunity_monitor.setup_history_covers_cursor",
                        return_value=(True, "ok"),
                    ):
                        with patch("app.services.opportunity_monitor.detect_skipped_closed_bars", return_value=[]):
                            with patch(
                                "app.services.opportunity_monitor._net_rr_ok",
                                AsyncMock(return_value=(True, 2.5)),
                            ):
                                summary = await mon.monitor_setups(session)
        await session.commit()
        assert summary.get("orders", 0) >= 1
        pos = (
            await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == "shadow-order-1"))
        ).scalar_one_or_none()
        assert pos is not None


@pytest.mark.asyncio
async def test_gap_suspend_preserves_balanced_pre_entry_ready():
    await init_db()
    async with SessionLocal() as session:
        from app.services.runtime_settings import get_runtime_settings

        rt = await get_runtime_settings(session)
        rt.paper_entry_mode = ENTRY_MODE_BALANCED
        await session.commit()

        setup = AiProposedSetup(
            setup_id="gap-ready-1",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=READY_LONG,
            monitoring_enabled=True,
            monitor_from_ms=0,
            monitor_cursor_ms=0,
            entry_price=100.0,
            entry_zone_low=ZONE_LO,
            entry_zone_high=ZONE_HI,
            stop_loss=90.0,
            take_profit=130.0,
            invalidation_level=89.0,
            confirmation_state={
                "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "required": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "completed": ["liquidity_sweep", "mss"],
                "missing": ["entry_zone_touch"],
                "confirmation_timestamps": {"liquidity_sweep": 1, "mss": 2},
                "confirmation_engine": "BALANCED",
            },
            structured_snapshot={"confirmation_type": "sweep_mss_poi"},
        )
        session.add(setup)
        await session.commit()

        mon = OpportunityMonitorService()
        bundle = KlineBundle(closed=[], forming=None, gaps_detected=[], gaps_remaining=[], data_complete=False, interval="5m")
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle)):
            with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(False, 999, 330)):
                await mon.monitor_setups(session)
        await session.commit()
        row = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "gap-ready-1"))).scalar_one()
        assert row.lifecycle_state == READY_LONG


@pytest.mark.asyncio
async def test_1m_integrity_gap_from_cursor_fails():
    bars = [
        Candle(time_ms=1_000_000, open=1, high=2, low=1, close=1.5, volume=1),
        Candle(time_ms=1_180_000, open=1.5, high=2, low=1, close=1.6, volume=1),
    ]
    ok, reason, _ = assess_1m_monitor_integrity(
        closed_1m=bars,
        data_complete=True,
        gaps_remaining=[],
        boundary_ms=900_000,
        cursor_ms=1_060_000,
        now_ms=1_250_000,
    )
    assert not ok
    assert reason == "1m_gap_from_cursor"
