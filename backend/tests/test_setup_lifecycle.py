from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.domain.candles import Candle
from app.domain.setup_lifecycle import READY_LONG, WAIT_LONG
from app.services.conditional_setup import parse_conditional_plan
from app.services.ict_confirmation_engine import evaluate_confirmations


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
        "confirmation_required": "Wait for liquidity sweep and MSS",
        "conditional_invalidation": "Close below 94",
    }
    plan, err = parse_conditional_plan(structured)
    assert err is None
    assert plan is not None
    assert plan["potential_direction"] == "LONG"
    assert "liquidity_sweep" in plan["required_confirmations"]
    assert "mss" in plan["required_confirmations"]


def test_wait_to_ready_requires_confirmations():
    candles = [
        Candle(time_ms=i * 300_000, open=100, high=101, low=99, close=100.5, volume=1.0)
        for i in range(20)
    ]
    candles[-1] = Candle(time_ms=19 * 300_000, open=100, high=102, low=98, close=101.5, volume=2.0)
    progress = evaluate_confirmations(
        direction="LONG",
        required=["entry_zone_touch"],
        completed_prior=[],
        candles=candles,
        zone_lo=99.0,
        zone_hi=101.0,
        invalidation_level=94.0,
        structured={},
    )
    assert "entry_zone_touch" in progress.completed


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
        "stop_loss": 210.0,
        "take_profit": 180.0,
        "proposed_stop_loss": 210.0,
        "proposed_take_profit": 180.0,
        "confirmation_required": "MSS after sweep",
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
        loaded = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == row.setup_id))).scalar_one()
        assert loaded.conditional_plan.get("potential_direction") == "SHORT"
