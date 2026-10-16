from __future__ import annotations

from app.domain.opportunity_miss_reasons import (
    OUTCOME_CONFIRMATION_INCOMPLETE,
    OUTCOME_REPLAY_SEQUENCE_LIMITED,
    REPLAY_STATUS_OK,
    REPLAY_STATUS_SEQUENCE_LIMITED,
)
from app.domain.setup_lifecycle import REASON_MISSING_CONFIRMATIONS, WAIT_LONG
from app.services.opportunity_miss_report import classify_setup_outcome
from app.services.revision_replay import assess_revision_replay_trust


def test_staging_setup_legacy_revision_limits_replay():
    """Mimics setup 29af5bb2: sequence revised without merge snapshot in history."""
    conf = {
        "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
        "required": ["liquidity_sweep", "mss", "entry_zone_touch"],
        "confirmation_timestamps": {
            "mss": 1_700_000_100_000,
            "fvg_retest": 1_700_000_200_000,
        },
        "confirmation_evidence": {},
    }
    notes = {
        "revision_history": [
            {
                "kind": "conditional_revision",
                "source_run_id": 42,
                "prior_levels": {"entry_zone_low": 60000.0, "entry_zone_high": 60100.0},
                "new_levels": {"entry_zone_low": 59900.0, "entry_zone_high": 60050.0},
                "confirmation_sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
            }
        ]
    }
    status, rev_notes, seed = assess_revision_replay_trust(
        confirmation_state=conf,
        objective_notes=notes,
    )
    assert status == REPLAY_STATUS_SEQUENCE_LIMITED
    assert seed is None
    assert rev_notes

    outcome = classify_setup_outcome(
        type(
            "Setup",
            (),
            {
                "lifecycle_state": WAIT_LONG,
                "last_rejection_reason": REASON_MISSING_CONFIRMATIONS,
                "objective_notes": notes,
                "last_validation_result": {},
            },
        )(),
        timeline={"paper_orders": []},
        replay_diff={
            "replay_status": status,
            "replay_trusted": False,
            "missing_in_live": ["liquidity_sweep"],
        },
    )
    assert outcome == OUTCOME_REPLAY_SEQUENCE_LIMITED


def test_unified_revision_with_snapshot_allows_seeded_replay():
    conf = {
        "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
        "confirmation_timestamps": {"mss": 1_700_000_100_000},
        "confirmation_evidence": {},
    }
    snapshot = {
        "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
        "confirmation_timestamps": {"mss": 1_700_000_100_000},
        "confirmation_evidence": {},
    }
    notes = {
        "revision_history": [
            {
                "kind": "conditional_revision",
                "impact": "sequence",
                "sequence_before": ["mss", "fvg_retest", "entry_zone_touch"],
                "sequence_after": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "confirmation_snapshot_after_merge": snapshot,
            }
        ]
    }
    status, _, seed = assess_revision_replay_trust(confirmation_state=conf, objective_notes=notes)
    assert status == REPLAY_STATUS_OK
    assert seed is not None
    assert seed.get("confirmation_timestamps", {}).get("mss") == 1_700_000_100_000


def test_live_replay_match_chronology_is_advisory_not_primary_bucket():
    setup = type(
        "Setup",
        (),
        {
            "lifecycle_state": WAIT_LONG,
            "last_rejection_reason": REASON_MISSING_CONFIRMATIONS,
            "objective_notes": {},
            "last_validation_result": {},
        },
    )()
    replay_diff = {
        "replay_status": REPLAY_STATUS_OK,
        "replay_trusted": True,
        "missing_in_live": [],
        "extra_in_live": [],
        "order_mismatch": False,
        "same_bar_multi_token_candidates": [
            {
                "tokens_on_5m_close_window": ["mss", "entry_zone_touch"],
                "chronology_provable_on_1m": False,
                "bar_open_ms": 1_000_000,
            }
        ],
    }
    assert (
        classify_setup_outcome(setup, timeline={"paper_orders": []}, replay_diff=replay_diff)
        == OUTCOME_CONFIRMATION_INCOMPLETE
    )
