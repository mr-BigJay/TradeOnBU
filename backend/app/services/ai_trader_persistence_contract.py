"""Align GPT JSON output with backend persistence / objective monitor requirements."""

from __future__ import annotations

from typing import Any

from app.services.ai_trader_report import _has_conditional_plan
from app.services.conditional_setup import (
    parse_conditional_plan,
    resolve_immediate_confirmation_sequence,
    _parse_float,
)

ALLOWED_CONFIRMATION_TYPES = frozenset(
    {
        "trend_pullback_resume",
        "level_breakout_retest",
        "sweep_mss_poi",
        "sweep_bos_poi",
        "bos_fvg_retest",
        "mss_fvg_retest",
    }
)


def validate_structured_for_persistence(structured: dict[str, Any]) -> str | None:
    """
    Return an error code when structured output cannot be persisted safely.
    Conditional WAIT plans must satisfy parse_conditional_plan (fail closed).
    Immediate LONG/SHORT must include monitor fields or persistence is rejected.
    """
    if not isinstance(structured, dict):
        return "structured_not_object"

    from app.services.entry_strategies import validate_strategy_plan
    err = validate_strategy_plan(structured)
    if err:
        return err

    if _has_conditional_plan(structured):
        _, err = parse_conditional_plan(structured)
        return err

    decision = str(structured.get("decision", "NO_TRADE")).upper()
    if decision in ("LONG", "SHORT"):
        seq, seq_err = resolve_immediate_confirmation_sequence(structured)
        inv = _parse_float(
            structured.get("structural_invalidation_level") or structured.get("invalidation_level")
        )
        if not seq:
            return seq_err or "immediate_missing_monitor_requirements"
        if inv is None:
            return "missing_structural_invalidation_level"
    return None


def contract_hint_for_model() -> str:
    """Short reminder embedded in instructions (kept in sync with validators)."""
    types = ", ".join(sorted(ALLOWED_CONFIRMATION_TYPES))
    return (
        f"confirmation_type ({types}) OR confirmation_sequence exactly matching a profile; "
        "structural_invalidation_level (numeric) required for conditional WAIT and immediate LONG/SHORT"
    )
