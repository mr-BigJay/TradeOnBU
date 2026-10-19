"""Execution pipeline metadata (live state only — shadow uses balanced_shadow_sim)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.domain.entry_execution import (
    ENTRY_MODE_BALANCED,
    PIPELINE_AI_VALIDATED,
    PIPELINE_ENTRY_ELIGIBLE,
    PIPELINE_ORDER_PENDING,
    PIPELINE_POSITION_OPEN,
    normalize_entry_mode,
)
from app.domain.setup_lifecycle import PENDING_ENTRY, POSITION_OPEN
from app.services.confirmation_policy import ConfirmationPolicy, promotion_ready, resolve_confirmation_policy


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


def policy_for_setup(setup: Any, sequence: list[str]) -> ConfirmationPolicy:
    return resolve_confirmation_policy(
        sequence=sequence,
        structured=setup.structured_snapshot or {},
        plan=setup.conditional_plan or {},
    )


def sync_pipeline_after_monitor(
    setup: Any,
    *,
    entry_mode: str,
    shadow: bool,
    policy: ConfirmationPolicy,
    confirmation_state: dict[str, Any],
) -> None:
    mode = normalize_entry_mode(entry_mode)
    lc = (setup.lifecycle_state or "").upper()

    state = PIPELINE_AI_VALIDATED
    if lc == POSITION_OPEN:
        state = PIPELINE_POSITION_OPEN
    elif lc == PENDING_ENTRY:
        state = PIPELINE_ORDER_PENDING
    elif mode == ENTRY_MODE_BALANCED and promotion_ready(
        policy, confirmation_state, allow_pre_entry=True
    ):
        state = PIPELINE_ENTRY_ELIGIBLE

    set_execution_pipeline(
        setup,
        {
            "state": state,
            "entry_mode": mode,
            "shadow_mode": bool(shadow),
            "updated_at": _utc_now_iso(),
            "pre_entry_ready": promotion_ready(policy, confirmation_state, allow_pre_entry=True)
            and not promotion_ready(policy, confirmation_state, allow_pre_entry=False),
            "full_confirmations": promotion_ready(
                policy, confirmation_state, allow_pre_entry=False
            ),
        },
    )


def strict_promotion_ready(
    *,
    policy: ConfirmationPolicy,
    confirmation_state: dict[str, Any],
) -> bool:
    return promotion_ready(policy, confirmation_state, allow_pre_entry=False)


def balanced_promotion_ready(
    *,
    policy: ConfirmationPolicy,
    confirmation_state: dict[str, Any],
    allow_pre_entry: bool,
) -> bool:
    return promotion_ready(policy, confirmation_state, allow_pre_entry=allow_pre_entry)
