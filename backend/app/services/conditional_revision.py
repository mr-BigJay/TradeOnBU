"""Merge ChatGPT conditional revisions without discarding valid ICT confirmations."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from app.domain.setup_lifecycle import (
    SETUP_MODE_CONDITIONAL,
    ICT_CONFIRMED_LONG,
    ICT_CONFIRMED_SHORT,
    PENDING_ENTRY,
    POSITION_OPEN,
    READY_LONG,
    READY_SHORT,
    REASON_MISSING_CONFIRMATIONS,
    wait_state_for_direction,
)
from app.services.conditional_setup import apply_conditional_plan_to_setup_row


class RevisionImpact(str, Enum):
    NONE = "none"
    LEVELS = "levels"
    SEQUENCE = "sequence"
    LEVELS_AND_SEQUENCE = "levels_and_sequence"
    STRUCTURAL = "structural"


def _absolute_level_tolerance(ref: float) -> float:
    """Structural level match for SL/TP/entry/invalidation."""
    return max(25.0, abs(ref) * 0.0005)


def _entry_zone_boundary_tolerance(ref: float) -> float:
    """Tighter tolerance for entry zone bounds (narrow BTC POI zones)."""
    return max(5.0, abs(ref) * 0.00015)


def _levels_equivalent(a: float | None, b: float | None, *, ref: float) -> bool:
    if a is None or b is None:
        return False
    tol = _absolute_level_tolerance(ref)
    return abs(float(a) - float(b)) <= tol


def _entry_zones_equivalent(
    row: Any,
    plan: dict[str, Any],
    *,
    ref: float,
) -> bool:
    zone_lo = plan.get("entry_zone_low")
    zone_hi = plan.get("entry_zone_high")
    if (
        row.entry_zone_low is None
        or row.entry_zone_high is None
        or zone_lo is None
        or zone_hi is None
    ):
        return False
    old_lo, old_hi = float(row.entry_zone_low), float(row.entry_zone_high)
    new_lo, new_hi = float(zone_lo), float(zone_hi)
    tol = _entry_zone_boundary_tolerance(ref)
    if abs(old_lo - new_lo) > tol or abs(old_hi - new_hi) > tol:
        return False
    overlap = min(old_hi, new_hi) - max(old_lo, new_lo)
    if overlap <= 0:
        return False
    min_width = min(old_hi - old_lo, new_hi - new_lo)
    if min_width <= 0:
        return False
    return overlap >= min_width * 0.5


def assess_conditional_revision_impact(row: Any, plan: dict[str, Any]) -> RevisionImpact:
    direction = str(plan.get("potential_direction") or "").upper()
    if str(row.direction or "").upper() != direction:
        return RevisionImpact.STRUCTURAL

    ref = float(plan.get("proposed_entry_mid") or row.entry_price or 1.0)

    inv = plan.get("structural_invalidation_level")
    if row.invalidation_level is not None and inv is not None:
        if not _levels_equivalent(float(row.invalidation_level), float(inv), ref=ref):
            return RevisionImpact.STRUCTURAL

    zone_changed = not _entry_zones_equivalent(row, plan, ref=ref)

    sl = plan.get("proposed_stop_loss")
    tp = plan.get("proposed_take_profit")
    entry = plan.get("proposed_entry_mid")
    levels_changed = False
    if entry is not None and sl is not None and tp is not None:
        levels_changed = not (
            row.entry_price is not None
            and row.stop_loss is not None
            and row.take_profit is not None
            and _levels_equivalent(float(row.entry_price), float(entry), ref=ref)
            and _levels_equivalent(float(row.stop_loss), float(sl), ref=ref)
            and _levels_equivalent(float(row.take_profit), float(tp), ref=ref)
        )

    old_seq = _sequence_from_row(row)
    new_seq = list(plan.get("confirmation_sequence") or [])
    sequence_changed = old_seq != new_seq
    levels_or_zone_changed = zone_changed or levels_changed

    if levels_or_zone_changed and sequence_changed:
        return RevisionImpact.LEVELS_AND_SEQUENCE
    if levels_or_zone_changed:
        return RevisionImpact.LEVELS
    if sequence_changed:
        return RevisionImpact.SEQUENCE

    return RevisionImpact.NONE


def _sequence_from_row(row: Any) -> list[str]:
    conf = row.confirmation_state or {}
    seq = conf.get("required") or conf.get("sequence")
    if isinstance(seq, list) and seq:
        return [str(x) for x in seq]
    plan = row.conditional_plan or {}
    seq2 = plan.get("confirmation_sequence")
    if isinstance(seq2, list):
        return [str(x) for x in seq2]
    return []


def _old_sequence_from_state(old_state: dict[str, Any]) -> list[str]:
    seq = old_state.get("sequence") or old_state.get("required") or []
    if isinstance(seq, list):
        return [str(x) for x in seq]
    return []


def _merge_timestamps_for_sequence_revision(
    old_sequence: list[str],
    new_sequence: list[str],
    timestamps: dict[str, int],
) -> dict[str, int]:
    """Keep only a valid prefix of confirmations aligned with the new sequence."""
    prefix: dict[str, int] = {}
    for i, tok in enumerate(new_sequence):
        if i >= len(old_sequence) or old_sequence[i] != tok:
            break
        if tok not in timestamps:
            break
        prefix[tok] = int(timestamps[tok])

    ordered: dict[str, int] = {}
    last_ms = -1
    for tok in new_sequence:
        if tok not in prefix:
            break
        ms = prefix[tok]
        if ms < last_ms:
            break
        ordered[tok] = ms
        last_ms = ms
    return ordered


def _bar_intersects_zone(bar_low: float, bar_high: float, zone_lo: float, zone_hi: float) -> bool:
    return float(bar_high) >= float(zone_lo) and float(bar_low) <= float(zone_hi)


def entry_zone_touch_evidence_valid(
    evidence: dict[str, Any] | None,
    *,
    zone_lo: float,
    zone_hi: float,
) -> bool:
    if not evidence:
        return False
    try:
        blo = float(evidence["bar_low"])
        bhi = float(evidence["bar_high"])
        zlo = float(evidence["zone_lo"])
        zhi = float(evidence["zone_hi"])
    except (KeyError, TypeError, ValueError):
        return False
    if not _bar_intersects_zone(blo, bhi, zone_lo, zone_hi):
        return False
    if zlo != zone_lo or zhi != zone_hi:
        return _bar_intersects_zone(blo, bhi, zone_lo, zone_hi)
    return True


def _drop_sequence_suffix(
    sequence: list[str],
    timestamps: dict[str, int],
    evidence: dict[str, Any],
    start_idx: int,
) -> None:
    for tok in sequence[start_idx:]:
        timestamps.pop(tok, None)
        evidence.pop(tok, None)


def objective_confirmations_satisfied(row: Any) -> tuple[bool, str | None]:
    """Objective ICT sequence complete, ordered, and entry touch evidenced for current zone."""
    from app.services.ict_confirmation_engine import ConfirmationProgress, all_required_met

    conf = row.confirmation_state or {}
    sequence = conf.get("sequence") or conf.get("required") or []
    if not isinstance(sequence, list) or not sequence:
        return False, REASON_MISSING_CONFIRMATIONS

    progress = ConfirmationProgress(
        sequence=[str(x) for x in sequence],
        completed=[str(x) for x in (conf.get("completed") or [])],
        missing=[str(x) for x in (conf.get("missing") or [])],
        confirmation_timestamps=dict(conf.get("confirmation_timestamps") or {}),
    )
    if not all_required_met(progress):
        return False, REASON_MISSING_CONFIRMATIONS

    if "entry_zone_touch" in progress.sequence:
        timestamps = progress.confirmation_timestamps
        if "entry_zone_touch" in timestamps:
            if row.entry_zone_low is None or row.entry_zone_high is None:
                return False, REASON_MISSING_CONFIRMATIONS
            touch_ev = (conf.get("confirmation_evidence") or {}).get("entry_zone_touch")
            if not entry_zone_touch_evidence_valid(
                touch_ev,
                zone_lo=float(row.entry_zone_low),
                zone_hi=float(row.entry_zone_high),
            ):
                return False, REASON_MISSING_CONFIRMATIONS
    return True, None


def sanitize_entry_zone_touch_in_state(
    state: dict[str, Any],
    *,
    zone_lo: float | None,
    zone_hi: float | None,
) -> dict[str, Any]:
    if zone_lo is None or zone_hi is None:
        return state
    sequence = list(state.get("sequence") or state.get("required") or [])
    timestamps = dict(state.get("confirmation_timestamps") or {})
    evidence = dict(state.get("confirmation_evidence") or {})
    if "entry_zone_touch" not in timestamps:
        return state
    touch_ev = evidence.get("entry_zone_touch")
    if entry_zone_touch_evidence_valid(touch_ev, zone_lo=float(zone_lo), zone_hi=float(zone_hi)):
        return state
    if "entry_zone_touch" in sequence:
        idx = sequence.index("entry_zone_touch")
        _drop_sequence_suffix(sequence, timestamps, evidence, idx)
    out = {
        **state,
        "sequence": sequence,
        "required": sequence,
        "confirmation_timestamps": timestamps,
        "confirmation_evidence": evidence,
    }
    out["completed"] = [t for t in sequence if t in timestamps]
    out["missing"] = [t for t in sequence if t not in timestamps]
    return out


def merge_confirmation_state(
    old_state: dict[str, Any],
    new_sequence: list[str],
    impact: RevisionImpact,
) -> dict[str, Any]:
    timestamps = dict(old_state.get("confirmation_timestamps") or {})
    evidence = dict(old_state.get("confirmation_evidence") or {})
    old_sequence = _old_sequence_from_state(old_state)

    if impact == RevisionImpact.STRUCTURAL:
        timestamps = {}
        evidence = {}

    elif impact in (RevisionImpact.SEQUENCE, RevisionImpact.LEVELS_AND_SEQUENCE):
        timestamps = _merge_timestamps_for_sequence_revision(old_sequence, new_sequence, timestamps)
        evidence = {k: v for k, v in evidence.items() if k in timestamps}
        if impact == RevisionImpact.LEVELS_AND_SEQUENCE and "entry_zone_touch" in new_sequence:
            idx = new_sequence.index("entry_zone_touch")
            _drop_sequence_suffix(new_sequence, timestamps, evidence, idx)

    elif impact == RevisionImpact.LEVELS:
        if "entry_zone_touch" in new_sequence:
            idx = new_sequence.index("entry_zone_touch")
            _drop_sequence_suffix(new_sequence, timestamps, evidence, idx)
        else:
            timestamps = {k: v for k, v in timestamps.items() if k in new_sequence}
            evidence = {k: v for k, v in evidence.items() if k in new_sequence}

    elif impact == RevisionImpact.NONE:
        timestamps = {k: v for k, v in timestamps.items() if k in new_sequence}
        evidence = {k: v for k, v in evidence.items() if k in new_sequence}

    completed = [t for t in new_sequence if t in timestamps]
    missing = [t for t in new_sequence if t not in timestamps]
    return {
        "sequence": list(new_sequence),
        "required": list(new_sequence),
        "completed": completed,
        "missing": missing,
        "confirmation_timestamps": timestamps,
        "confirmation_evidence": evidence,
    }


_PRESERVE_LIFECYCLE = frozenset(
    {
        READY_LONG,
        READY_SHORT,
        ICT_CONFIRMED_LONG,
        ICT_CONFIRMED_SHORT,
        PENDING_ENTRY,
        POSITION_OPEN,
    }
)


def apply_conditional_revision_to_setup_row(
    row: Any,
    *,
    plan: dict[str, Any],
    run_id: int,
    structured: dict[str, Any],
    revision_at: datetime | None = None,
) -> RevisionImpact:
    """Apply a duplicate conditional revision; preserve monitor + confirmations when safe."""
    impact = assess_conditional_revision_impact(row, plan)
    old_lifecycle = row.lifecycle_state
    old_monitor_from = row.monitor_from_ms
    old_monitor_cursor = row.monitor_cursor_ms
    old_conf = dict(row.confirmation_state or {})
    revision_time = revision_at or datetime.now(timezone.utc)

    if impact == RevisionImpact.STRUCTURAL:
        apply_conditional_plan_to_setup_row(
            row,
            plan=plan,
            run_id=run_id,
            structured=structured,
            created_at=revision_time,
        )
        notes = dict(row.objective_notes or {})
        history = notes.setdefault("lifecycle_history", [])
        history.append(
            {
                "at": revision_time.isoformat(),
                "event": "conditional_revision",
                "impact": impact.value,
                "run_id": run_id,
            }
        )
        rev_hist = notes.setdefault("revision_history", [])
        rev_hist.append(
            {"run_id": run_id, "impact": impact.value, "at": revision_time.isoformat()}
        )
        row.objective_notes = notes
        return impact

    direction = plan["potential_direction"]
    sequence = list(plan["confirmation_sequence"])
    row.direction = direction
    row.setup_mode = SETUP_MODE_CONDITIONAL
    row.final_ai_decision = "WAIT"
    row.monitoring_enabled = True
    row.entry_zone_low = plan["entry_zone_low"]
    row.entry_zone_high = plan["entry_zone_high"]
    row.entry_price = plan["proposed_entry_mid"]
    row.stop_loss = plan["proposed_stop_loss"]
    row.take_profit = plan["proposed_take_profit"]
    row.invalidation_level = plan.get("structural_invalidation_level")
    row.invalidation = structured.get("conditional_invalidation") or structured.get("invalidation_condition")
    row.conditional_plan = plan
    row.structured_snapshot = structured
    row.run_id = run_id

    row.confirmation_state = merge_confirmation_state(old_conf, sequence, impact)
    row.confirmation_state = sanitize_entry_zone_touch_in_state(
        row.confirmation_state,
        zone_lo=row.entry_zone_low,
        zone_hi=row.entry_zone_high,
    )

    if impact in (RevisionImpact.NONE, RevisionImpact.LEVELS, RevisionImpact.LEVELS_AND_SEQUENCE):
        row.monitor_from_ms = old_monitor_from
        row.monitor_cursor_ms = old_monitor_cursor
    elif impact == RevisionImpact.SEQUENCE and old_monitor_from is not None:
        row.monitor_from_ms = old_monitor_from
        row.monitor_cursor_ms = old_monitor_cursor

    conf_ok, reject_reason = objective_confirmations_satisfied(row)
    if not conf_ok:
        row.lifecycle_state = wait_state_for_direction(direction)
        row.last_rejection_reason = reject_reason or REASON_MISSING_CONFIRMATIONS
        row.last_validation_result = {
            "ok": False,
            "reason": reject_reason or REASON_MISSING_CONFIRMATIONS,
            "missing": row.confirmation_state.get("missing") or [],
            "ict_confirmed": False,
        }
    elif not row.confirmation_state.get("missing") and old_lifecycle in _PRESERVE_LIFECYCLE:
        row.lifecycle_state = old_lifecycle
    elif not row.confirmation_state.get("missing"):
        row.lifecycle_state = wait_state_for_direction(direction)
    else:
        row.lifecycle_state = wait_state_for_direction(direction)

    notes = dict(row.objective_notes or {})
    history = notes.setdefault("lifecycle_history", [])
    history.append(
        {
            "at": datetime.now(timezone.utc).isoformat(),
            "event": "conditional_revision",
            "impact": impact.value,
            "run_id": run_id,
        }
    )
    rev_hist = notes.setdefault("revision_history", [])
    rev_hist.append({"run_id": run_id, "impact": impact.value, "at": datetime.now(timezone.utc).isoformat()})
    row.objective_notes = notes
    return impact
