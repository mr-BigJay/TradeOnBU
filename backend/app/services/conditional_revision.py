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
    wait_state_for_direction,
)
from app.services.conditional_setup import apply_conditional_plan_to_setup_row


class RevisionImpact(str, Enum):
    NONE = "none"
    LEVELS = "levels"
    SEQUENCE = "sequence"
    STRUCTURAL = "structural"


def _absolute_level_tolerance(ref: float) -> float:
    """Structural level match: small absolute band, not loose %-only similarity."""
    return max(25.0, abs(ref) * 0.0005)


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
    if not _levels_equivalent(old_lo, new_lo, ref=ref):
        return False
    if not _levels_equivalent(old_hi, new_hi, ref=ref):
        return False
    overlap = min(old_hi, new_hi) - max(old_lo, new_lo)
    return overlap > 0


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

    if zone_changed or levels_changed:
        return RevisionImpact.LEVELS

    old_seq = _sequence_from_row(row)
    new_seq = list(plan.get("confirmation_sequence") or [])
    if old_seq != new_seq:
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


def merge_confirmation_state(
    old_state: dict[str, Any],
    new_sequence: list[str],
    impact: RevisionImpact,
) -> dict[str, Any]:
    timestamps = dict(old_state.get("confirmation_timestamps") or {})

    if impact == RevisionImpact.STRUCTURAL:
        timestamps = {}

    elif impact == RevisionImpact.SEQUENCE:
        timestamps = {k: v for k, v in timestamps.items() if k in new_sequence}

    elif impact == RevisionImpact.LEVELS:
        if "entry_zone_touch" in new_sequence:
            idx = new_sequence.index("entry_zone_touch")
            for tok in new_sequence[idx:]:
                timestamps.pop(tok, None)
        else:
            timestamps = {k: v for k, v in timestamps.items() if k in new_sequence}

    elif impact == RevisionImpact.NONE:
        timestamps = {k: v for k, v in timestamps.items() if k in new_sequence}

    completed = [t for t in new_sequence if t in timestamps]
    missing = [t for t in new_sequence if t not in timestamps]
    return {
        "sequence": list(new_sequence),
        "required": list(new_sequence),
        "completed": completed,
        "missing": missing,
        "confirmation_timestamps": timestamps,
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
    created_at: datetime | None = None,
) -> RevisionImpact:
    """Apply a duplicate conditional revision; preserve monitor + confirmations when safe."""
    impact = assess_conditional_revision_impact(row, plan)
    old_lifecycle = row.lifecycle_state
    old_monitor_from = row.monitor_from_ms
    old_monitor_cursor = row.monitor_cursor_ms
    old_conf = dict(row.confirmation_state or {})

    if impact == RevisionImpact.STRUCTURAL:
        apply_conditional_plan_to_setup_row(
            row, plan=plan, run_id=run_id, structured=structured, created_at=created_at
        )
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

    if impact in (RevisionImpact.NONE, RevisionImpact.LEVELS):
        row.monitor_from_ms = old_monitor_from
        row.monitor_cursor_ms = old_monitor_cursor
    elif impact == RevisionImpact.SEQUENCE and old_monitor_from is not None:
        row.monitor_from_ms = old_monitor_from
        row.monitor_cursor_ms = old_monitor_cursor

    if impact == RevisionImpact.NONE and old_lifecycle in _PRESERVE_LIFECYCLE:
        row.lifecycle_state = old_lifecycle
    elif not row.confirmation_state.get("missing"):
        row.lifecycle_state = old_lifecycle if old_lifecycle in _PRESERVE_LIFECYCLE else wait_state_for_direction(direction)
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
