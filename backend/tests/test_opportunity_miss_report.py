from __future__ import annotations

from types import SimpleNamespace

from app.domain.opportunity_miss_reasons import (
    OUTCOME_CONFIRMATION_INCOMPLETE,
    OUTCOME_NET_RR_BELOW_MIN,
    OUTCOME_POTENTIAL_MISSED,
)
from app.domain.setup_lifecycle import REASON_MISSING_CONFIRMATIONS, WAIT_LONG
from app.services.opportunity_miss_report import classify_setup_outcome


def test_classify_potential_missed_when_replay_differs():
    setup = SimpleNamespace(
        lifecycle_state=WAIT_LONG,
        last_rejection_reason=REASON_MISSING_CONFIRMATIONS,
        objective_notes={},
        last_validation_result={},
    )
    outcome = classify_setup_outcome(
        setup,
        timeline={"paper_orders": []},
        replay_diff={
            "missing_in_live": ["mss"],
            "order_mismatch": False,
            "replay_status": "ok",
            "replay_trusted": True,
        },
    )
    assert outcome == OUTCOME_POTENTIAL_MISSED


def test_classify_confirmation_incomplete_without_replay_gap():
    setup = SimpleNamespace(
        lifecycle_state=WAIT_LONG,
        last_rejection_reason=REASON_MISSING_CONFIRMATIONS,
        objective_notes={},
        last_validation_result={},
    )
    outcome = classify_setup_outcome(setup, timeline={"paper_orders": []}, replay_diff=None)
    assert outcome == OUTCOME_CONFIRMATION_INCOMPLETE


def test_classify_net_rr_blocked():
    setup = SimpleNamespace(
        lifecycle_state="ICT_CONFIRMED_LONG",
        last_rejection_reason="ict_confirmed_blocked_by_net_rr",
        objective_notes={},
        last_validation_result={},
    )
    outcome = classify_setup_outcome(setup, timeline={"paper_orders": []}, replay_diff=None)
    assert outcome == OUTCOME_NET_RR_BELOW_MIN
