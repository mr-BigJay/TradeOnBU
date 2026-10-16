"""Hardening: levels-only revisions, chained revisions, missing snapshots."""

from __future__ import annotations

from app.domain.opportunity_miss_reasons import REPLAY_STATUS_OK, REPLAY_STATUS_SEQUENCE_LIMITED
from app.services.revision_replay import assess_revision_replay_trust


def _live_conf() -> dict:
    return {
        "sequence": ["mss", "entry_zone_touch"],
        "required": ["mss", "entry_zone_touch"],
        "confirmation_timestamps": {"mss": 1_000_000},
        "confirmation_evidence": {},
    }


def test_levels_only_revision_with_snapshot_seeds_replay():
    """Entry zone changes with stable sequence must not replay from setup birth."""
    conf = _live_conf()
    merge_snapshot = {
        "sequence": ["mss", "entry_zone_touch"],
        "confirmation_timestamps": {"mss": 1_000_000},
        "confirmation_evidence": {},
    }
    notes = {
        "revision_history": [
            {
                "impact": "levels",
                "at": "2024-06-01T10:00:00+00:00",
                "sequence_before": ["mss", "entry_zone_touch"],
                "sequence_after": ["mss", "entry_zone_touch"],
                "prior_levels": {"entry_zone_low": 100.0, "entry_zone_high": 101.0},
                "new_levels": {"entry_zone_low": 99.5, "entry_zone_high": 100.5},
                "confirmation_snapshot_after_merge": merge_snapshot,
            }
        ]
    }
    status, _, seed, replay_from = assess_revision_replay_trust(
        confirmation_state=conf,
        objective_notes=notes,
        monitor_from_ms=1_000_000,
    )
    assert status == REPLAY_STATUS_OK
    assert seed is not None
    assert replay_from is not None
    assert replay_from >= 1_300_000


def test_chained_revisions_use_latest_effective_snapshot():
    conf = _live_conf()
    snap1 = {
        "sequence": ["mss", "entry_zone_touch"],
        "confirmation_timestamps": {"mss": 1_000_000},
    }
    snap2 = {
        "sequence": ["mss", "entry_zone_touch"],
        "confirmation_timestamps": {"mss": 1_600_000},
    }
    notes = {
        "revision_history": [
            {
                "impact": "sequence",
                "at": "2024-06-01T09:00:00+00:00",
                "sequence_before": ["entry_zone_touch"],
                "sequence_after": ["mss", "entry_zone_touch"],
                "confirmation_snapshot_after_merge": snap1,
            },
            {
                "impact": "levels",
                "at": "2024-06-01T12:00:00+00:00",
                "sequence_before": ["mss", "entry_zone_touch"],
                "sequence_after": ["mss", "entry_zone_touch"],
                "prior_levels": {"entry_zone_low": 100.0, "entry_zone_high": 101.0},
                "new_levels": {"entry_zone_low": 99.0, "entry_zone_high": 101.0},
                "confirmation_snapshot_after_merge": snap2,
            },
        ]
    }
    status, _, seed, replay_from = assess_revision_replay_trust(
        confirmation_state=conf,
        objective_notes=notes,
        monitor_from_ms=1_000_000,
    )
    assert status == REPLAY_STATUS_OK
    assert seed is not None
    assert seed["confirmation_timestamps"]["mss"] == 1_600_000
    assert replay_from is not None
    assert replay_from >= 1_900_000


def test_levels_revision_without_snapshot_is_fail_closed():
    conf = _live_conf()
    notes = {
        "revision_history": [
            {
                "impact": "levels",
                "at": "2024-06-01T10:00:00+00:00",
                "sequence_before": ["mss", "entry_zone_touch"],
                "sequence_after": ["mss", "entry_zone_touch"],
                "prior_levels": {"entry_zone_low": 100.0, "entry_zone_high": 101.0},
                "new_levels": {"entry_zone_low": 99.0, "entry_zone_high": 101.0},
            }
        ]
    }
    status, out_notes, seed, replay_from = assess_revision_replay_trust(
        confirmation_state=conf,
        objective_notes=notes,
        monitor_from_ms=1_000_000,
    )
    assert status == REPLAY_STATUS_SEQUENCE_LIMITED
    assert seed is None
    assert replay_from is None
    assert any("without_snapshot" in n for n in out_notes)
