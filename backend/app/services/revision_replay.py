"""Revision-aware replay eligibility for miss audit."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.domain.candle_utils import interval_ms
from app.domain.opportunity_miss_reasons import (
    REPLAY_STATUS_OK,
    REPLAY_STATUS_SEQUENCE_LIMITED,
)

_CONFIRMATION_IMPACTS = frozenset({"levels", "sequence", "levels_and_sequence", "structural"})


def _seq_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw]


def _sequence_from_conf(conf: dict[str, Any]) -> list[str]:
    return _seq_list(conf.get("sequence") or conf.get("required"))


def sanitize_conf_snapshot(conf: dict[str, Any]) -> dict[str, Any]:
    seq = _sequence_from_conf(conf)
    ts = dict(conf.get("confirmation_timestamps") or {})
    ev = dict(conf.get("confirmation_evidence") or {})
    ts = {k: int(v) for k, v in ts.items() if k in seq}
    ev = {k: v for k, v in ev.items() if k in ts}
    completed = [t for t in seq if t in ts]
    missing = [t for t in seq if t not in ts]
    return {
        "sequence": seq,
        "required": seq,
        "completed": completed,
        "missing": missing,
        "confirmation_timestamps": ts,
        "confirmation_evidence": ev,
    }


def confirmation_timestamps_compatible_with_sequence(sequence: list[str], timestamps: dict[str, int]) -> tuple[bool, str | None]:
    """Live monitor state: orphans and out-of-order hits are invalid; missing prefix tokens are OK after revision."""
    if not sequence:
        return True, None
    for tok in timestamps:
        if tok not in sequence:
            return False, f"orphan_timestamp_{tok}"
    ordered: list[tuple[int, str]] = []
    for i, tok in enumerate(sequence):
        if tok in timestamps:
            ordered.append((int(timestamps[tok]), tok))
    for j in range(len(ordered) - 1):
        if ordered[j][0] > ordered[j + 1][0]:
            return False, "timestamps_out_of_order"
    return True, None


def _parse_revision_at_ms(entry: dict[str, Any]) -> int | None:
    for key in ("at", "revised_at"):
        raw = entry.get(key)
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except (TypeError, ValueError):
            continue
    return None


def _levels_changed(prior: dict[str, Any], new_lv: dict[str, Any]) -> bool:
    for key in ("entry_zone_low", "entry_zone_high", "entry_price", "stop_loss", "take_profit", "invalidation_level"):
        if prior.get(key) != new_lv.get(key):
            return True
    return False


def _entry_affects_confirmations(entry: dict[str, Any]) -> bool:
    impact = str(entry.get("impact") or "").lower()
    if impact in _CONFIRMATION_IMPACTS:
        return True
    seq_before = _seq_list(entry.get("sequence_before"))
    seq_after = _seq_list(entry.get("sequence_after") or entry.get("confirmation_sequence"))
    if seq_before and seq_after and seq_before != seq_after:
        return True
    prior_levels = entry.get("prior_levels") if isinstance(entry.get("prior_levels"), dict) else {}
    new_levels = entry.get("new_levels") if isinstance(entry.get("new_levels"), dict) else {}
    if prior_levels and new_levels and _levels_changed(prior_levels, new_levels):
        return True
    return False


def compute_snapshot_replay_from_ms(
    *,
    monitor_from_ms: int,
    seed: dict[str, Any],
    revision_at_ms: int | None,
    interval: str = "5m",
) -> int:
    """
    First 5m bar open at which seeded replay may evaluate new confirmations.

    Skips candles before the revision instant and before the last merged confirmation bar.
    """
    step = interval_ms(interval)
    ts = dict(seed.get("confirmation_timestamps") or {})
    after_last_conf = monitor_from_ms
    if ts:
        after_last_conf = max(int(v) for v in ts.values()) + step
    rev_floor = monitor_from_ms
    if revision_at_ms is not None:
        rev_floor = revision_at_ms
    return max(monitor_from_ms, after_last_conf, rev_floor)


def assess_revision_replay_trust(
    *,
    confirmation_state: dict[str, Any],
    objective_notes: dict[str, Any] | None,
    monitor_from_ms: int = 0,
) -> tuple[str, list[str], dict[str, Any] | None, int | None]:
    """
    Return (replay_status, notes, optional seeded confirmation_state for replay, replay_from_ms).

    Any confirmation-affecting revision requires a post-merge snapshot and timestamp; the latest
    such revision anchors seeded replay. Otherwise fail-closed (no full-history replay on new levels).
    """
    notes: list[str] = []
    conf = confirmation_state or {}
    seq = _sequence_from_conf(conf)
    ts = dict(conf.get("confirmation_timestamps") or {})

    ok, reason = confirmation_timestamps_compatible_with_sequence(seq, ts)
    if not ok:
        notes.append(reason or "incompatible_live_timestamps")
        return REPLAY_STATUS_SEQUENCE_LIMITED, notes, None, None

    rev_hist = list((objective_notes or {}).get("revision_history") or [])
    if not rev_hist:
        return REPLAY_STATUS_OK, notes, None, None

    seed_candidates: list[tuple[int, dict[str, Any]]] = []

    for entry in rev_hist:
        kind = str(entry.get("kind") or "")
        seq_before = _seq_list(entry.get("sequence_before"))
        seq_after = _seq_list(entry.get("sequence_after") or entry.get("confirmation_sequence"))
        snapshot = entry.get("confirmation_snapshot_after_merge")
        prior_levels = entry.get("prior_levels") if isinstance(entry.get("prior_levels"), dict) else {}
        new_levels = entry.get("new_levels") if isinstance(entry.get("new_levels"), dict) else {}

        if kind == "conditional_revision" and seq_after and not seq_before:
            notes.append("legacy_revision_missing_sequence_before")
            return REPLAY_STATUS_SEQUENCE_LIMITED, notes, None, None

        if kind == "conditional_revision" and seq_after and seq_after != seq:
            notes.append("legacy_revision_sequence_differs_from_live_state")
            return REPLAY_STATUS_SEQUENCE_LIMITED, notes, None, None

        if prior_levels and new_levels and _levels_changed(prior_levels, new_levels) and not snapshot:
            notes.append("levels_change_without_snapshot")
            return REPLAY_STATUS_SEQUENCE_LIMITED, notes, None, None

        if seq_before and seq_after and seq_before != seq_after and not snapshot:
            notes.append(f"sequence_change_{seq_before}_to_{seq_after}_without_snapshot")
            return REPLAY_STATUS_SEQUENCE_LIMITED, notes, None, None

        if not _entry_affects_confirmations(entry):
            continue

        if not snapshot:
            impact = str(entry.get("impact") or "").lower() or "unknown"
            notes.append(f"confirmation_affecting_revision_{impact}_without_snapshot")
            return REPLAY_STATUS_SEQUENCE_LIMITED, notes, None, None

        at_ms = _parse_revision_at_ms(entry)
        if at_ms is None:
            notes.append("confirmation_affecting_revision_missing_timestamp")
            return REPLAY_STATUS_SEQUENCE_LIMITED, notes, None, None

        seed_candidates.append((at_ms, sanitize_conf_snapshot(snapshot)))

    if not seed_candidates:
        return REPLAY_STATUS_OK, notes, None, None

    seed_revision_at_ms, seeded = max(seed_candidates, key=lambda item: item[0])
    seeded = sanitize_conf_snapshot(seeded)
    if seq and _sequence_from_conf(seeded) != seq:
        notes.append("snapshot_sequence_mismatch")
        return REPLAY_STATUS_SEQUENCE_LIMITED, notes, None, None

    replay_from_ms = compute_snapshot_replay_from_ms(
        monitor_from_ms=monitor_from_ms,
        seed=seeded,
        revision_at_ms=seed_revision_at_ms,
    )
    return REPLAY_STATUS_OK, notes, seeded, replay_from_ms
