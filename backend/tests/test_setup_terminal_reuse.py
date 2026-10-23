"""A new proposal must never inherit a terminal opportunity's execution ID."""
from copy import deepcopy

import pytest

from app.db import SessionLocal, init_db
from app.models import SimPosition
from app.services.ai_setup_memory import create_setup_from_structured
from app.services.opportunity_monitor import _sync_position_lifecycle
from tests.test_ai_trader_persistence_contract import (
    _production_conditional_wait_long,
    _production_immediate_long_monitored,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["POSITION_CLOSED", "INVALIDATED", "EXPIRED", "NO_TRADE"])
async def test_conditional_proposal_gets_new_id_after_terminal_setup(terminal):
    await init_db()
    async with SessionLocal() as session:
        data = _production_conditional_wait_long()
        old, _ = await create_setup_from_structured(
            session, run_id=1, symbol="BTCUSDT", structured=data
        )
        old.lifecycle_state = terminal
        old.status = "open"  # Reproduce the legacy inconsistent production row.
        position = SimPosition(
            public_id="prior-closed-position", symbol="BTCUSDT", direction="LONG",
            status="tp_hit", margin_usdt=10, leverage=5,
            entry_price=100, stop_loss=94, take_profit=115,
            ai_setup_id=old.setup_id,
        )
        session.add(position)
        await session.commit()
        before = deepcopy(old.objective_notes)
        old_expiry = old.expires_at
        new, meta = await create_setup_from_structured(
            session, run_id=2, symbol="BTCUSDT", structured=data
        )
        assert meta["created"] and not meta["updated"]
        assert new.setup_id != old.setup_id
        assert new.lifecycle_state == "WAIT_LONG"
        assert new.confirmation_state["completed"] == []
        assert new.monitor_cursor_ms < new.monitor_from_ms
        await _sync_position_lifecycle(session, new)
        assert new.lifecycle_state == "WAIT_LONG"
        assert old.lifecycle_state == terminal
        assert old.run_id == 1
        assert old.objective_notes == before
        assert old.expires_at == old_expiry
        assert position.ai_setup_id == old.setup_id


@pytest.mark.asyncio
async def test_explicit_continue_cannot_revise_closed_setup():
    await init_db()
    async with SessionLocal() as session:
        old, _ = await create_setup_from_structured(
            session, run_id=1, symbol="BTCUSDT",
            structured=_production_conditional_wait_long(),
        )
        old.lifecycle_state = "POSITION_CLOSED"
        await session.commit()
        before = deepcopy(old.objective_notes)
        data = _production_immediate_long_monitored()
        data["proposed_setup"] = {"continue_setup_id": old.setup_id}
        row, meta = await create_setup_from_structured(
            session, run_id=2, symbol="BTCUSDT", structured=data
        )
        assert row is None
        assert meta["reason"] == "continue_setup_terminal"
        assert old.run_id == 1
        assert old.objective_notes == before
