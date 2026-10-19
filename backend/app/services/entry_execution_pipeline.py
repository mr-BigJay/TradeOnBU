"""Execution pipeline state on AiProposedSetup (objective_notes.execution_pipeline)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.domain.entry_execution import (
    ENTRY_MODE_BALANCED,
    ENTRY_MODE_STRICT,
    PIPELINE_AI_VALIDATED,
    PIPELINE_ENTRY_ELIGIBLE,
    PIPELINE_ORDER_PENDING,
    PIPELINE_POSITION_OPEN,
    normalize_entry_mode,
)
from app.domain.setup_lifecycle import PENDING_ENTRY, POSITION_OPEN
from app.services.balanced_confirmation_engine import (
    pre_entry_requirements_met,
    required_confirmations_met,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_execution_pipeline(setup: Any) -> dict[str, Any]:
    notes = setup.objective_notes or {}
    pipe = notes.get("execution_pipeline")
    return dict(pipe) if isinstance(pipe, dict) else {}


def set_execution_pipeline(setup: Any, patch: dict[str, Any]) -> None:
    notes = dict(setup.objective_notes or {})
    current = get_execution_pipeline(setup)
    current.update(patch)
    notes["execution_pipeline"] = current
    setup.objective_notes = notes


def sync_pipeline_after_monitor(
    setup: Any,
    *,
    entry_mode: str,
    shadow: bool,
    sequence: list[str],
    confirmation_state: dict[str, Any],
) -> None:
    mode = normalize_entry_mode(entry_mode)
    structured = setup.structured_snapshot or {}
    lc = (setup.lifecycle_state or "").upper()

    if lc == POSITION_OPEN:
        state = PIPELINE_POSITION_OPEN
    elif lc == PENDING_ENTRY:
        state = PIPELINE_ORDER_PENDING
    elif mode == ENTRY_MODE_BALANCED and pre_entry_requirements_met(
        sequence=sequence,
        confirmation_state=confirmation_state,
        structured=structured,
    ):
        full = required_confirmations_met(
            sequence=sequence,
            confirmation_state=confirmation_state,
            structured=structured,
        )
        state = PIPELINE_ENTRY_ELIGIBLE if not full else PIPELINE_ENTRY_ELIGIBLE
        if full and "entry_zone_touch" in (confirmation_state.get("confirmation_timestamps") or {}):
            state = PIPELINE_ENTRY_ELIGIBLE
    else:
        state = PIPELINE_AI_VALIDATED

    if required_confirmations_met(
        sequence=sequence,
        confirmation_state=confirmation_state,
        structured=structured,
    ):
        if lc in (PENDING_ENTRY,):
            state = PIPELINE_ORDER_PENDING
        elif lc == POSITION_OPEN:
            state = PIPELINE_POSITION_OPEN
        elif mode == ENTRY_MODE_BALANCED and pre_entry_requirements_met(
            sequence=sequence,
            confirmation_state=confirmation_state,
            structured=structured,
        ):
            state = PIPELINE_ENTRY_ELIGIBLE

    set_execution_pipeline(
        setup,
        {
            "state": state,
            "entry_mode": mode,
            "shadow_mode": bool(shadow),
            "updated_at": _utc_now_iso(),
            "pre_entry_ready": pre_entry_requirements_met(
                sequence=sequence,
                confirmation_state=confirmation_state,
                structured=structured,
            ),
            "full_confirmations": required_confirmations_met(
                sequence=sequence,
                confirmation_state=confirmation_state,
                structured=structured,
            ),
        },
    )


def strict_promotion_ready(
    *,
    sequence: list[str],
    confirmation_state: dict[str, Any],
    structured: dict[str, Any] | None,
) -> bool:
    from app.services.ict_confirmation_engine import all_required_met, ConfirmationProgress

    progress = ConfirmationProgress(
        sequence=list(sequence),
        completed=[str(x) for x in (confirmation_state.get("completed") or [])],
        missing=[str(x) for x in (confirmation_state.get("missing") or [])],
        confirmation_timestamps=dict(confirmation_state.get("confirmation_timestamps") or {}),
    )
    return all_required_met(progress)


def balanced_promotion_ready(
    *,
    sequence: list[str],
    confirmation_state: dict[str, Any],
    structured: dict[str, Any] | None,
    allow_pre_entry: bool,
) -> bool:
    structured = structured or {}
    if required_confirmations_met(
        sequence=sequence,
        confirmation_state=confirmation_state,
        structured=structured,
    ):
        return True
    if allow_pre_entry and pre_entry_requirements_met(
        sequence=sequence,
        confirmation_state=confirmation_state,
        structured=structured,
    ):
        return True
    return False
