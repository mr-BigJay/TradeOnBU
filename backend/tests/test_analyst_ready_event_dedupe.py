"""Deterministic lifecycle regressions for repeated READY and stale pending orders.

These are state tests; they do not claim historical trade performance.
"""
from types import SimpleNamespace

import pytest

from app.domain.setup_lifecycle import EXPIRED, PENDING_ENTRY
from app.services.opportunity_monitor import (
    _new_ready_transition,
    _sync_position_lifecycle,
)


def _ready_setup():
    return SimpleNamespace(
        direction="SHORT",
        entry_price=65181.0,
        stop_loss=65650.0,
        take_profit=64450.0,
        invalidation_level=65500.0,
        confirmation_state={
            "confirmation_timestamps": {"htf_pullback_rejection": 1794927600000}
        },
        structured_snapshot={"verified_trade_plan": {"candidate_id": "synthetic-b"}},
        objective_notes={},
    )


def test_same_proof_and_geometry_only_emits_one_ready_transition():
    setup = _ready_setup()
    assert _new_ready_transition(setup)
    assert not _new_ready_transition(setup)
    assert setup.objective_notes["last_ready_transition"]["entry"] == 65181.0


def test_new_proof_or_changed_levels_can_emit_new_transition():
    setup = _ready_setup()
    assert _new_ready_transition(setup)
    setup.confirmation_state = {
        "confirmation_timestamps": {"htf_pullback_rejection": 1794927900000}
    }
    assert _new_ready_transition(setup)
    assert not _new_ready_transition(setup)
    setup.entry_price = 65150.0
    assert _new_ready_transition(setup)


@pytest.mark.asyncio
async def test_expired_setup_is_not_revived_by_uncancelled_pending_limit():
    class SessionStub:
        async def execute(self, _query):
            return SimpleNamespace(
                scalar_one_or_none=lambda: SimpleNamespace(status="pending_entry")
            )

    row = SimpleNamespace(
        setup_id="synthetic-expired",
        status="expired",
        lifecycle_state=PENDING_ENTRY,
    )
    await _sync_position_lifecycle(SessionStub(), row)
    assert row.lifecycle_state == EXPIRED


@pytest.mark.asyncio
async def test_open_setup_still_tracks_its_existing_pending_limit():
    class SessionStub:
        async def execute(self, _query):
            return SimpleNamespace(
                scalar_one_or_none=lambda: SimpleNamespace(status="pending_entry")
            )

    row = SimpleNamespace(
        setup_id="synthetic-active",
        status="open",
        lifecycle_state="WAIT_SHORT",
    )
    await _sync_position_lifecycle(SessionStub(), row)
    assert row.lifecycle_state == PENDING_ENTRY
