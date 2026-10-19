"""Paper entry execution modes and pipeline states (STRICT vs BALANCED)."""

from __future__ import annotations

from enum import Enum

# Dashboard / runtime settings
ENTRY_MODE_STRICT = "STRICT"
ENTRY_MODE_BALANCED = "BALANCED"
ENTRY_MODES = frozenset({ENTRY_MODE_STRICT, ENTRY_MODE_BALANCED})

# Independent pipeline (stored on setup objective_notes.execution_pipeline)
PIPELINE_AI_VALIDATED = "AI_VALIDATED"
PIPELINE_ENTRY_ELIGIBLE = "ENTRY_ELIGIBLE"
PIPELINE_ORDER_PENDING = "ORDER_PENDING"
PIPELINE_POSITION_OPEN = "POSITION_OPEN"

# Miss audit — user-facing opportunity buckets (mapped in opportunity_miss_report)
AUDIT_DIRECTION_CORRECT = "direction_correct_no_entry"
AUDIT_POTENTIAL_NOT_EXECUTABLE = "potential_not_executable"
AUDIT_MISSED_EXECUTABLE = "missed_executable_opportunity"
AUDIT_ORDER_UNFILLED = "order_created_not_filled"
AUDIT_EXECUTED_PROFIT = "executed_profitable_trade"
AUDIT_EXECUTED_LOSS = "executed_loss_trade"
AUDIT_EXECUTED_OPEN = "executed_open_or_breakeven"


class EntryExecutionMode(str, Enum):
    STRICT = ENTRY_MODE_STRICT
    BALANCED = ENTRY_MODE_BALANCED


def normalize_entry_mode(raw: str | None) -> str:
    if raw and str(raw).upper() == ENTRY_MODE_BALANCED:
        return ENTRY_MODE_BALANCED
    return ENTRY_MODE_STRICT
