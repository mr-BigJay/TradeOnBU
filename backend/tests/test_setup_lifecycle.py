from __future__ import annotations

import pytest

from app.domain.setup_lifecycle import READY_LONG, WAIT_LONG
from app.services.conditional_setup import parse_conditional_plan
from app.services.ict_confirmation_engine import process_bar_sequential
from app.domain.candles import Candle


def test_parse_conditional_plan_success():
    structured = {
        "decision": "NO_TRADE",
        "final_ai_decision": "WAIT",
        "setup_mode": "conditional",
        "potential_direction": "LONG",
        "conditional_entry_zone_low": 99.0,
        "conditional_entry_zone_high": 101.0,
        "proposed_stop_loss": 95.0,
        "proposed_take_profit": 110.0,
        "structural_invalidation_level": 94.0,
        "confirmation_type": "sweep_mss_poi",
    }
    plan, err = parse_conditional_plan(structured)
    assert err is None
    assert plan is not None
    assert plan["potential_direction"] == "LONG"
    assert plan["confirmation_sequence"] == ["liquidity_sweep", "mss", "entry_zone_touch"]


def test_parse_conditional_fails_without_confirmation_profile():
    structured = {
        "decision": "NO_TRADE",
        "final_ai_decision": "WAIT",
        "setup_mode": "conditional",
        "potential_direction": "LONG",
        "conditional_entry_zone_low": 99.0,
        "conditional_entry_zone_high": 101.0,
        "proposed_stop_loss": 95.0,
        "proposed_take_profit": 110.0,
        "structural_invalidation_level": 94.0,
    }
    _, err = parse_conditional_plan(structured)
    assert err == "missing_structured_confirmation_type"


def test_parse_conditional_fails_without_invalidation():
    structured = {
        "decision": "NO_TRADE",
        "final_ai_decision": "WAIT",
        "setup_mode": "conditional",
        "potential_direction": "LONG",
        "conditional_entry_zone_low": 99.0,
        "conditional_entry_zone_high": 101.0,
        "proposed_stop_loss": 95.0,
        "proposed_take_profit": 110.0,
        "confirmation_type": "sweep_mss_poi",
    }
    _, err = parse_conditional_plan(structured)
    assert err == "missing_structural_invalidation_level"


def test_lifecycle_constants():
    assert WAIT_LONG == "WAIT_LONG"
    assert READY_LONG == "READY_LONG"


@pytest.mark.asyncio
async def test_conditional_setup_persisted_from_structured():
    from sqlalchemy import select

    from app.db import SessionLocal, init_db
    from app.models import AiProposedSetup
    from app.services.ai_setup_memory import create_setup_from_structured

    await init_db()
    structured = {
        "decision": "NO_TRADE",
        "final_ai_decision": "WAIT",
        "setup_mode": "conditional",
        "potential_direction": "SHORT",
        "conditional_entry_zone_low": 200.0,
        "conditional_entry_zone_high": 205.0,
        "proposed_stop_loss": 210.0,
        "proposed_take_profit": 180.0,
        "structural_invalidation_level": 215.0,
        "confirmation_type": "sweep_mss_poi",
    }
    async with SessionLocal() as session:
        row, meta = await create_setup_from_structured(
            session, run_id=1, symbol="BTCUSDT", structured=structured
        )
        await session.commit()
        assert meta["created"] is True
        assert row is not None
        assert row.lifecycle_state == "WAIT_SHORT"
        assert row.setup_mode == "conditional"
        assert row.monitor_from_ms > 0
        loaded = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == row.setup_id))).scalar_one()
        assert loaded.conditional_plan.get("confirmation_sequence")
