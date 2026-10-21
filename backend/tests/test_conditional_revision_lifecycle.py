"""Regression: conditional revisions must not wipe ICT confirmations or READY state."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.domain.setup_lifecycle import (
    ICT_CONFIRMED_SHORT,
    READY_LONG,
    READY_SHORT,
    REASON_ICT_CONFIRMED_BLOCKED_RR,
    WAIT_LONG,
    WAIT_SHORT,
)
from app.models import AiProposedSetup, SimPositionSettings
from app.services.ai_setup_memory import create_setup_from_structured
from app.services.conditional_revision import (
    RevisionImpact,
    assess_conditional_revision_impact,
    merge_confirmation_state,
)


def _short_setup_structured(**overrides):
    base = {
        "decision": "WAIT",
        "setup_mode": "conditional",
        "potential_direction": "SHORT",
        "market_bias": "SHORT",
        "conditional_entry_zone_low": 82300.0,
        "conditional_entry_zone_high": 82400.0,
        "proposed_stop_loss": 82650.0,
        "proposed_take_profit": 81730.0,
        "structural_invalidation_level": 82800.0,
        "confirmation_type": "sweep_mss_poi",
        "confirmation_sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
        "expires_hours": 48,
        "trade_reason": "test",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_revision_without_structural_change_preserves_confirmations():
    await init_db()
    s1 = _short_setup_structured()
    s2 = _short_setup_structured(trade_reason="same thesis re-run")
    async with SessionLocal() as session:
        row, meta1 = await create_setup_from_structured(session, run_id=50, symbol="BTCUSDT", structured=s1)
        assert row is not None
        row.lifecycle_state = READY_SHORT
        row.confirmation_state = {
            "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
            "required": ["liquidity_sweep", "mss", "entry_zone_touch"],
            "completed": ["liquidity_sweep", "mss", "entry_zone_touch"],
            "missing": [],
            "confirmation_timestamps": {
                "liquidity_sweep": 1_700_000_000_000,
                "mss": 1_700_000_300_000,
                "entry_zone_touch": 1_700_000_600_000,
            },
            "confirmation_evidence": {
                "entry_zone_touch": {
                    "bar_low": 82310.0,
                    "bar_high": 82390.0,
                    "zone_lo": 82300.0,
                    "zone_hi": 82400.0,
                }
            },
        }
        row.monitor_from_ms = 1_700_000_000_000
        row.monitor_cursor_ms = 1_700_000_600_000
        await session.commit()

        row2, meta2 = await create_setup_from_structured(session, run_id=57, symbol="BTCUSDT", structured=s2)
        await session.commit()
        assert meta2.get("updated") is True
        assert meta2.get("revision_impact") == RevisionImpact.NONE.value
        assert row2.setup_id == row.setup_id
        assert row2.lifecycle_state == READY_SHORT
        assert row2.confirmation_state.get("missing") == []
        assert len(row2.confirmation_state.get("completed") or []) == 3
        assert row2.monitor_cursor_ms == 1_700_000_600_000


@pytest.mark.asyncio
async def test_revision_sequence_change_does_not_inherit_invalid_confirmations():
    await init_db()
    s1 = _short_setup_structured()
    s2 = _short_setup_structured(
        confirmation_type="sweep_bos_poi",
        confirmation_sequence=["liquidity_sweep", "bos", "entry_zone_touch"],
    )
    async with SessionLocal() as session:
        row, _ = await create_setup_from_structured(session, run_id=1, symbol="BTCUSDT", structured=s1)
        row.confirmation_state = {
            "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
            "required": ["liquidity_sweep", "mss", "entry_zone_touch"],
            "completed": ["liquidity_sweep", "mss", "entry_zone_touch"],
            "missing": [],
            "confirmation_timestamps": {
                "liquidity_sweep": 100,
                "mss": 200,
                "entry_zone_touch": 300,
            },
        }
        await session.commit()
        row2, meta = await create_setup_from_structured(session, run_id=2, symbol="BTCUSDT", structured=s2)
        await session.commit()
        assert meta.get("revision_impact") == RevisionImpact.SEQUENCE.value
        assert "mss" not in (row2.confirmation_state.get("completed") or [])
        assert "liquidity_sweep" in (row2.confirmation_state.get("completed") or [])
        assert "bos" in (row2.confirmation_state.get("missing") or [])
        assert "entry_zone_touch" not in (row2.confirmation_state.get("completed") or [])
        assert "entry_zone_touch" not in (row2.confirmation_state.get("confirmation_timestamps") or {})


def test_ict_confirmed_state_distinct_from_wait():
    from app.domain.setup_lifecycle import ict_confirmed_state_for_direction, ready_state_for_direction

    assert ict_confirmed_state_for_direction("SHORT") == ICT_CONFIRMED_SHORT
    assert ready_state_for_direction("SHORT") == READY_SHORT
    assert ICT_CONFIRMED_SHORT != WAIT_SHORT


@pytest.mark.asyncio
async def test_ready_with_auto_paper_off_does_not_create_order(session):
    await init_db()
    from app.models import SimPosition

    setup = AiProposedSetup(
        setup_id="ready-no-auto",
        run_id=9,
        symbol="BTCUSDT",
        direction="LONG",
        status="open",
        lifecycle_state=READY_LONG,
        monitoring_enabled=True,
        entry_price=100.0,
        entry_zone_low=99.0,
        entry_zone_high=101.0,
        stop_loss=94.0,
        take_profit=115.0,
        invalidation_level=93.0,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
        confirmation_state={
            "sequence": ["entry_zone_touch"],
            "required": ["entry_zone_touch"],
            "completed": ["entry_zone_touch"],
            "missing": [],
            "confirmation_timestamps": {"entry_zone_touch": 1_700_000_000_000},
            "confirmation_evidence": {
                "entry_zone_touch": {
                    "bar_low": 99.5,
                    "bar_high": 100.5,
                    "zone_lo": 99.0,
                    "zone_hi": 101.0,
                }
            },
        },
    )
    session.add(setup)
    session.add(SimPositionSettings(id=1, auto_open_paper_positions=False, initial_balance_set=True))
    await session.commit()

    from unittest.mock import AsyncMock, patch
    from app.services.opportunity_monitor import _attempt_ready_paper_order

    from app.domain.candle_utils import candle_close_ms, interval_ms
    from app.domain.candles import Candle

    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    step = interval_ms("5m")
    open_ms = (now_ms // step) * step - step
    closed_bar = Candle(time_ms=open_ms, open=100, high=101, low=99, close=100, volume=1.0)
    assert now_ms >= candle_close_ms(closed_bar, "5m")

    summary = {"orders": 0}
    with patch("app.services.opportunity_monitor._net_rr_ok", AsyncMock(return_value=(True, 2.2))):
        ok = await _attempt_ready_paper_order(
            session,
            setup,
            now=now,
            summary=summary,
            latest_closed_5m=closed_bar,
            market_data_verified=True,
        )
    assert ok is False
    assert setup.lifecycle_state == READY_LONG
    count = (await session.execute(select(SimPosition))).scalars().all()
    assert not count


def test_assess_impact_none_for_duplicate_levels():
    from app.models import AiProposedSetup

    row = AiProposedSetup(
        direction="SHORT",
        entry_price=82365.0,
        entry_zone_low=82300.0,
        entry_zone_high=82400.0,
        stop_loss=82650.0,
        take_profit=81730.0,
        invalidation_level=82800.0,
        confirmation_state={"required": ["liquidity_sweep", "mss", "entry_zone_touch"]},
    )
    plan = {
        "potential_direction": "SHORT",
        "confirmation_sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
        "proposed_entry_mid": 82365.0,
        "entry_zone_low": 82300.0,
        "entry_zone_high": 82400.0,
        "proposed_stop_loss": 82650.0,
        "proposed_take_profit": 81730.0,
        "structural_invalidation_level": 82800.0,
    }
    assert assess_conditional_revision_impact(row, plan) == RevisionImpact.NONE


def test_btc_entry_zone_shift_100_usd_is_levels_not_none():
    from app.models import AiProposedSetup

    row = AiProposedSetup(
        direction="SHORT",
        entry_price=82365.0,
        entry_zone_low=82300.0,
        entry_zone_high=82400.0,
        stop_loss=82650.0,
        take_profit=81730.0,
        invalidation_level=82800.0,
        confirmation_state={
            "required": ["liquidity_sweep", "mss", "entry_zone_touch"],
            "confirmation_timestamps": {
                "liquidity_sweep": 1,
                "mss": 2,
                "entry_zone_touch": 3,
            },
        },
    )
    plan = {
        "potential_direction": "SHORT",
        "confirmation_sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
        "proposed_entry_mid": 82415.0,
        "entry_zone_low": 82400.0,
        "entry_zone_high": 82500.0,
        "proposed_stop_loss": 82650.0,
        "proposed_take_profit": 81730.0,
        "structural_invalidation_level": 82800.0,
    }
    assert assess_conditional_revision_impact(row, plan) == RevisionImpact.LEVELS
    merged = merge_confirmation_state(
        row.confirmation_state,
        plan["confirmation_sequence"],
        RevisionImpact.LEVELS,
    )
    assert "entry_zone_touch" not in merged["completed"]
    assert "entry_zone_touch" in merged["missing"]


def test_invalidation_change_wins_over_sequence_change():
    from app.models import AiProposedSetup

    row = AiProposedSetup(
        direction="SHORT",
        entry_price=82365.0,
        entry_zone_low=82300.0,
        entry_zone_high=82400.0,
        stop_loss=82650.0,
        take_profit=81730.0,
        invalidation_level=82800.0,
        confirmation_state={"required": ["liquidity_sweep", "mss", "entry_zone_touch"]},
    )
    plan = {
        "potential_direction": "SHORT",
        "confirmation_sequence": ["liquidity_sweep", "bos", "entry_zone_touch"],
        "proposed_entry_mid": 82365.0,
        "entry_zone_low": 82300.0,
        "entry_zone_high": 82400.0,
        "proposed_stop_loss": 82650.0,
        "proposed_take_profit": 81730.0,
        "structural_invalidation_level": 82950.0,
    }
    assert assess_conditional_revision_impact(row, plan) == RevisionImpact.STRUCTURAL


def test_narrow_btc_zone_shift_is_levels_and_drops_stale_touch():
    from app.models import AiProposedSetup
    from app.services.conditional_revision import (
        entry_zone_touch_evidence_valid,
        merge_confirmation_state,
    )

    row = AiProposedSetup(
        direction="SHORT",
        entry_price=82375.0,
        entry_zone_low=82350.0,
        entry_zone_high=82380.0,
        stop_loss=82650.0,
        take_profit=81730.0,
        invalidation_level=82800.0,
        conditional_plan={
            "confirmation_sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
        },
    )
    plan = {
        "potential_direction": "SHORT",
        "confirmation_sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
        "proposed_entry_mid": 82385.0,
        "entry_zone_low": 82370.0,
        "entry_zone_high": 82400.0,
        "proposed_stop_loss": 82650.0,
        "proposed_take_profit": 81730.0,
        "structural_invalidation_level": 82800.0,
    }
    assert assess_conditional_revision_impact(row, plan) == RevisionImpact.LEVELS
    old_state = {
        "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
        "confirmation_timestamps": {
            "liquidity_sweep": 100,
            "mss": 200,
            "entry_zone_touch": 300,
        },
        "confirmation_evidence": {
            "entry_zone_touch": {
                "bar_low": 82355.0,
                "bar_high": 82360.0,
                "zone_lo": 82350.0,
                "zone_hi": 82380.0,
            }
        },
    }
    merged = merge_confirmation_state(old_state, plan["confirmation_sequence"], RevisionImpact.LEVELS)
    assert "entry_zone_touch" not in merged["confirmation_timestamps"]
    assert not entry_zone_touch_evidence_valid(
        old_state["confirmation_evidence"]["entry_zone_touch"],
        zone_lo=82370.0,
        zone_hi=82400.0,
    )


def test_sequence_revision_keeps_valid_prefix_only():
    from app.services.conditional_revision import merge_confirmation_state

    old_state = {
        "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
        "confirmation_timestamps": {"liquidity_sweep": 100, "mss": 200, "entry_zone_touch": 300},
    }
    new_seq = ["liquidity_sweep", "bos", "entry_zone_touch"]
    merged = merge_confirmation_state(old_state, new_seq, RevisionImpact.SEQUENCE)
    assert merged["confirmation_timestamps"] == {"liquidity_sweep": 100}
    assert merged["missing"] == ["bos", "entry_zone_touch"]


def test_levels_and_sequence_combined_drops_stale_middle_confirmations():
    from app.models import AiProposedSetup
    from app.services.conditional_revision import merge_confirmation_state
    from app.services.ict_confirmation_engine import sequence_order_valid

    row = AiProposedSetup(
        direction="LONG",
        entry_price=100.0,
        entry_zone_low=99.0,
        entry_zone_high=101.0,
        stop_loss=94.0,
        take_profit=115.0,
        invalidation_level=93.0,
        confirmation_state={
            "sequence": ["bos", "fvg_retest", "entry_zone_touch"],
            "required": ["bos", "fvg_retest", "entry_zone_touch"],
        },
    )
    plan = {
        "potential_direction": "LONG",
        "confirmation_sequence": ["mss", "fvg_retest", "entry_zone_touch"],
        "proposed_entry_mid": 101.25,
        "entry_zone_low": 100.5,
        "entry_zone_high": 102.0,
        "proposed_stop_loss": 94.0,
        "proposed_take_profit": 115.0,
        "structural_invalidation_level": 93.0,
    }
    assert assess_conditional_revision_impact(row, plan) == RevisionImpact.LEVELS_AND_SEQUENCE
    old_state = {
        "sequence": ["bos", "fvg_retest", "entry_zone_touch"],
        "confirmation_timestamps": {"bos": 100, "fvg_retest": 200, "entry_zone_touch": 300},
        "confirmation_evidence": {
            "entry_zone_touch": {
                "bar_low": 99.8,
                "bar_high": 100.2,
                "zone_lo": 99.0,
                "zone_hi": 101.0,
            }
        },
    }
    merged = merge_confirmation_state(
        old_state, plan["confirmation_sequence"], RevisionImpact.LEVELS_AND_SEQUENCE
    )
    assert merged["confirmation_timestamps"] == {}
    assert merged["missing"] == ["mss", "fvg_retest", "entry_zone_touch"]


@pytest.mark.asyncio
async def test_levels_and_sequence_revision_monitor_not_stuck_invalid_order():
    await init_db()
    from unittest.mock import AsyncMock, patch

    from app.domain.candle_utils import candle_close_ms
    from app.domain.setup_lifecycle import WAIT_LONG
    from app.domain.setup_monitor_boundary import first_eligible_bar_open_ms
    from app.services.conditional_revision import apply_conditional_revision_to_setup_row
    from app.services.ict_confirmation_engine import sequence_order_valid
    from app.services.market_data import KlineBundle
    from app.services.opportunity_monitor import OpportunityMonitorService
    from tests.test_lifecycle_realistic_pipeline import _5m

    created = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    boundary = first_eligible_bar_open_ms(created, "5m")

    async with SessionLocal() as session:
        row, _ = await create_setup_from_structured(
            session,
            run_id=80,
            symbol="BTCUSDT",
            structured=_long_bos_fvg_structured(),
        )
        row.created_at = created
        row.monitor_from_ms = boundary
        row.monitor_cursor_ms = boundary - 1
        row.confirmation_state = {
            "sequence": ["bos", "fvg_retest", "entry_zone_touch"],
            "required": ["bos", "fvg_retest", "entry_zone_touch"],
            "completed": ["bos", "fvg_retest", "entry_zone_touch"],
            "missing": [],
            "confirmation_timestamps": {
                "bos": boundary,
                "fvg_retest": boundary + 300_000,
                "entry_zone_touch": boundary + 600_000,
            },
            "confirmation_evidence": {
                "entry_zone_touch": {
                    "bar_low": 99.8,
                    "bar_high": 100.2,
                    "zone_lo": 99.0,
                    "zone_hi": 101.0,
                }
            },
        }
        await session.commit()

        plan = {
            "potential_direction": "LONG",
            "confirmation_sequence": ["mss", "fvg_retest", "entry_zone_touch"],
        "entry_zone_low": 100.5,
        "entry_zone_high": 102.0,
        "proposed_entry_mid": 101.25,
            "proposed_stop_loss": 94.0,
            "proposed_take_profit": 115.0,
            "structural_invalidation_level": 93.0,
        }
        structured = _long_mss_fvg_structured()
        impact = apply_conditional_revision_to_setup_row(
            row,
            plan=plan,
            run_id=81,
            structured=structured,
            revision_at=datetime(2026, 10, 10, 12, 30, tzinfo=timezone.utc),
        )
        await session.commit()
        assert impact == RevisionImpact.LEVELS_AND_SEQUENCE
        ts = row.confirmation_state.get("confirmation_timestamps") or {}
        assert "fvg_retest" not in ts
        assert ts == {}

        bars = [
            _5m(boundary, o=100.0, h=100.3, l=99.0, c=100.1),
            _5m(boundary + 300_000, o=100.0, h=100.2, l=98.6, c=99.4),
        ]
        bundle = KlineBundle(
            closed=bars,
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="5m",
        )
        now_ms = candle_close_ms(bars[-1], "5m") + 10_000
        mon = OpportunityMonitorService()
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle)):
            with patch("app.services.opportunity_monitor._utc_now") as mock_now:
                mock_now.return_value = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
                with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 10, 600)):
                    await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.commit()
        row = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == row.setup_id))).scalar_one()
        assert row.lifecycle_state == WAIT_LONG
        progress_ts = row.confirmation_state.get("confirmation_timestamps") or {}
        assert "mss" in (row.confirmation_state.get("missing") or [])
        assert "fvg_retest" not in progress_ts
        last_ms = -1
        for tok in row.confirmation_state["sequence"]:
            if tok not in progress_ts:
                break
            assert progress_ts[tok] >= last_ms
            last_ms = progress_ts[tok]


def _long_bos_fvg_structured(**overrides):
    base = {
        "decision": "WAIT",
        "setup_mode": "conditional",
        "potential_direction": "LONG",
        "market_bias": "LONG",
        "conditional_entry_zone_low": 99.0,
        "conditional_entry_zone_high": 101.0,
        "proposed_stop_loss": 94.0,
        "proposed_take_profit": 115.0,
        "structural_invalidation_level": 93.0,
        "confirmation_type": "bos_fvg_retest",
        "expires_hours": 48,
        "trade_reason": "bos fvg test",
    }
    base.update(overrides)
    return base


def _long_mss_fvg_structured(**overrides):
    base = {
        "decision": "WAIT",
        "setup_mode": "conditional",
        "potential_direction": "LONG",
        "market_bias": "LONG",
        "conditional_entry_zone_low": 100.5,
        "conditional_entry_zone_high": 102.0,
        "proposed_stop_loss": 94.0,
        "proposed_take_profit": 115.0,
        "structural_invalidation_level": 93.0,
        "confirmation_type": "mss_fvg_retest",
        "expires_hours": 48,
        "trade_reason": "mss fvg revision",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_structural_revision_monitor_boundary_at_revision_time():
    await init_db()
    from app.domain.setup_monitor_boundary import first_eligible_bar_open_ms
    from app.services.conditional_revision import apply_conditional_revision_to_setup_row

    old_created = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    revision_time = datetime(2026, 10, 10, 14, 0, tzinfo=timezone.utc)
    old_boundary = first_eligible_bar_open_ms(old_created, "5m")
    new_boundary = first_eligible_bar_open_ms(revision_time, "5m")

    async with SessionLocal() as session:
        row, _ = await create_setup_from_structured(
            session, run_id=90, symbol="BTCUSDT", structured=_long_bos_fvg_structured()
        )
        row.created_at = old_created
        row.monitor_from_ms = old_boundary
        row.monitor_cursor_ms = old_boundary + 600_000
        row.confirmation_state = {
            "sequence": ["bos", "fvg_retest", "entry_zone_touch"],
            "required": ["bos", "fvg_retest", "entry_zone_touch"],
            "completed": ["bos", "fvg_retest", "entry_zone_touch"],
            "missing": [],
            "confirmation_timestamps": {
                "bos": old_boundary,
                "fvg_retest": old_boundary + 300_000,
                "entry_zone_touch": old_boundary + 600_000,
            },
        }
        await session.commit()

        structured = _long_bos_fvg_structured(
            potential_direction="SHORT",
            market_bias="SHORT",
            conditional_entry_zone_low=82300.0,
            conditional_entry_zone_high=82400.0,
            proposed_stop_loss=82650.0,
            proposed_take_profit=81730.0,
            structural_invalidation_level=82800.0,
            confirmation_type="sweep_mss_poi",
        )
        plan, err = __import__(
            "app.services.conditional_setup", fromlist=["parse_conditional_plan"]
        ).parse_conditional_plan(structured)
        assert err is None and plan is not None
        impact = apply_conditional_revision_to_setup_row(
            row, plan=plan, run_id=91, structured=structured, revision_at=revision_time
        )
        await session.commit()
        assert impact == RevisionImpact.STRUCTURAL
        assert row.monitor_from_ms == new_boundary
        assert row.monitor_cursor_ms == new_boundary - 1
        assert row.confirmation_state.get("confirmation_timestamps") == {}
        assert row.confirmation_state.get("missing") == plan["confirmation_sequence"]

        reloaded = (
            await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == row.setup_id))
        ).scalar_one()
        assert reloaded.monitor_from_ms == new_boundary


@pytest.mark.asyncio
async def test_revision_sequence_then_monitor_reaches_ready():
    await init_db()
    from unittest.mock import AsyncMock, patch

    from app.domain.candle_utils import candle_close_ms
    from app.domain.setup_lifecycle import READY_LONG, WAIT_LONG
    from app.domain.setup_monitor_boundary import first_eligible_bar_open_ms
    from app.models import SimPosition
    from app.services.market_data import KlineBundle
    from app.services.opportunity_monitor import OpportunityMonitorService
    from tests.test_ai_trader_persistence_contract import _production_conditional_wait_long
    from tests.test_lifecycle_realistic_pipeline import _sweep_mss_poi_long_bars

    created = datetime(2026, 10, 10, 12, 7, tzinfo=timezone.utc)
    boundary = first_eligible_bar_open_ms(created, "5m")
    structured = _production_conditional_wait_long()
    structured2 = {**structured, "confirmation_type": "sweep_bos_poi", "trade_reason": "mss to bos revision"}

    async with SessionLocal() as session:
        row, _ = await create_setup_from_structured(session, run_id=70, symbol="BTCUSDT", structured=structured)
        row.lifecycle_state = WAIT_LONG
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
            "confirmation_evidence": {
                "entry_zone_touch": {
                    "bar_low": 99.8,
                    "bar_high": 100.6,
                    "zone_lo": 99.0,
                    "zone_hi": 101.0,
                }
            },
        }
        await session.commit()

        row2, meta = await create_setup_from_structured(session, run_id=71, symbol="BTCUSDT", structured=structured2)
        await session.commit()
        assert meta.get("revision_impact") == RevisionImpact.SEQUENCE.value
        assert row2.lifecycle_state == WAIT_LONG
        assert row2.confirmation_state.get("missing") == ["bos", "entry_zone_touch"]
        assert "entry_zone_touch" not in (row2.confirmation_state.get("confirmation_timestamps") or {})

        closed_5m = _sweep_mss_poi_long_bars(boundary)
        last_bar = closed_5m[-1]
        now_ms = candle_close_ms(last_bar, "5m") + 15_000
        bundle = KlineBundle(
            closed=closed_5m,
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="5m",
        )
        mon = OpportunityMonitorService()
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle)):
            with patch("app.services.opportunity_monitor._utc_now") as mock_now:
                mock_now.return_value = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
                with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 10, 600)):
                    with patch(
                        "app.services.opportunity_monitor._net_rr_ok",
                        AsyncMock(return_value=(True, 2.05)),
                    ):
                        summary = await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.commit()

        row3 = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == row2.setup_id))).scalar_one()
        assert summary["promoted"] == 1
        assert row3.lifecycle_state == READY_LONG
        assert row3.confirmation_state.get("completed") == ["liquidity_sweep", "bos", "entry_zone_touch"]
        touch_ev = (row3.confirmation_state.get("confirmation_evidence") or {}).get("entry_zone_touch")
        assert touch_ev is not None
        assert touch_ev["zone_lo"] == 99.0 and touch_ev["zone_hi"] == 101.0
        assert (await session.execute(select(SimPosition))).scalars().all() == []
