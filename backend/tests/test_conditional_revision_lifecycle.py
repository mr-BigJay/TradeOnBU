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
        stop_loss=94.0,
        take_profit=115.0,
        invalidation_level=93.0,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
    )
    session.add(setup)
    session.add(SimPositionSettings(id=1, auto_open_paper_positions=False, initial_balance_set=True))
    await session.commit()

    from unittest.mock import AsyncMock, patch
    from app.services.opportunity_monitor import _attempt_ready_paper_order

    summary = {"orders": 0}
    with patch("app.services.opportunity_monitor._net_rr_ok", AsyncMock(return_value=(True, 2.2))):
        ok = await _attempt_ready_paper_order(session, setup, now=datetime.now(timezone.utc), summary=summary)
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
