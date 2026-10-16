from __future__ import annotations

import pytest

from app.services.conditional_setup import resolve_confirmation_sequence


def test_arbitrary_two_token_sequence_rejected():
    _, err = resolve_confirmation_sequence(
        {"confirmation_sequence": ["entry_zone_touch", "displacement"]}
    )
    assert err == "unapproved_confirmation_sequence"


def test_exact_profile_sequence_accepted():
    seq, err = resolve_confirmation_sequence(
        {"confirmation_sequence": ["liquidity_sweep", "mss", "entry_zone_touch"]}
    )
    assert err is None
    assert seq == ["liquidity_sweep", "mss", "entry_zone_touch"]


@pytest.mark.asyncio
async def test_unapproved_sequence_cannot_persist_conditional():
    from app.db import SessionLocal, init_db
    from app.services.ai_setup_memory import create_setup_from_structured

    await init_db()
    structured = {
        "decision": "NO_TRADE",
        "final_ai_decision": "WAIT",
        "setup_mode": "conditional",
        "potential_direction": "LONG",
        "conditional_entry_zone_low": 99.0,
        "conditional_entry_zone_high": 101.0,
        "confirmation_sequence": ["entry_zone_touch", "displacement"],
        "structural_invalidation_level": 93.0,
        "stop_loss": 94.0,
        "take_profit": 115.0,
    }
    async with SessionLocal() as session:
        row, meta = await create_setup_from_structured(session, run_id=1, symbol="BTCUSDT", structured=structured)
        assert row is None
        assert meta["reason"] == "unapproved_confirmation_sequence"
