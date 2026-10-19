"""Isolated BALANCED shadow simulation (does not mutate live STRICT state)."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

from app.domain.candles import Candle
from app.domain.entry_execution import ENTRY_MODE_BALANCED
from app.services.balanced_confirmation_engine import (
    apply_balanced_progress_to_state,
    process_balanced_new_closed_1m_bars,
)
from app.services.confirmation_policy import ConfirmationPolicy, promotion_ready, resolve_confirmation_policy


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _shadow_store(setup: Any) -> dict[str, Any]:
    notes = dict(setup.objective_notes or {})
    sim = notes.get("balanced_shadow_sim")
    return dict(sim) if isinstance(sim, dict) else {}


def _write_shadow(setup: Any, sim: dict[str, Any]) -> None:
    notes = dict(setup.objective_notes or {})
    notes["balanced_shadow_sim"] = sim
    setup.objective_notes = notes


def init_shadow_sim_if_needed(setup: Any, *, policy: ConfirmationPolicy, monitor_from_ms: int) -> dict[str, Any]:
    sim = _shadow_store(setup)
    if sim.get("confirmation_state"):
        return sim
    sim = {
        "engine": ENTRY_MODE_BALANCED,
        "started_at": _utc_now_iso(),
        "monitor_from_ms": monitor_from_ms,
        "monitor_1m_cursor_ms": monitor_from_ms - 1,
        "confirmation_state": {
            **policy.as_state_fields(),
            "completed": [],
            "missing": list(policy.sequence),
            "confirmation_timestamps": {},
            "confirmation_evidence": {},
            "monitor_1m_cursor_ms": monitor_from_ms - 1,
            "confirmation_engine": "BALANCED_SHADOW",
        },
        "lifecycle_state_sim": setup.lifecycle_state,
        "would_promote_ready": False,
        "would_create_order": False,
    }
    _write_shadow(setup, sim)
    return sim


def run_balanced_shadow_tick(
    setup: Any,
    *,
    policy: ConfirmationPolicy,
    new_1m_bars: list[Candle],
    closed_5m: list[Candle],
    closed_1m: list[Candle],
    zone_lo: float,
    zone_hi: float,
    invalidation_level: float | None,
    structured: dict[str, Any],
    monitor_from_ms: int,
    now_ms: int,
) -> dict[str, Any]:
    """Advance shadow BALANCED confirmations only inside objective_notes.balanced_shadow_sim."""
    sim = init_shadow_sim_if_needed(setup, policy=policy, monitor_from_ms=monitor_from_ms)
    conf = dict(sim.get("confirmation_state") or {})
    cursor = int(conf.get("monitor_1m_cursor_ms") or monitor_from_ms - 1)

    progress = process_balanced_new_closed_1m_bars(
        direction=setup.direction,
        policy=policy,
        confirmation_state=conf,
        new_1m_bars=new_1m_bars,
        closed_5m=closed_5m,
        closed_1m=closed_1m,
        zone_lo=zone_lo,
        zone_hi=zone_hi,
        invalidation_level=invalidation_level,
        structured=structured,
        monitor_from_ms=monitor_from_ms,
        now_ms=now_ms,
    )

    new_cursor = progress.last_bar_ms if progress.last_bar_ms > cursor else cursor
    conf = apply_balanced_progress_to_state(
        conf,
        progress,
        policy=policy,
        monitor_1m_cursor_ms=new_cursor,
    )
    conf["confirmation_engine"] = "BALANCED_SHADOW"

    would_ready = promotion_ready(policy, conf, allow_pre_entry=True)
    would_full = promotion_ready(policy, conf, allow_pre_entry=False)

    sim.update(
        {
            "confirmation_state": conf,
            "monitor_1m_cursor_ms": new_cursor,
            "updated_at": _utc_now_iso(),
            "would_promote_ready": would_ready,
            "would_create_order": would_ready,
            "would_pre_entry_only": would_ready and not would_full,
            "invalidated": bool(progress.invalidated),
        }
    )
    _write_shadow(setup, sim)
    return sim


def shadow_snapshot_for_audit(setup: Any) -> dict[str, Any] | None:
    sim = _shadow_store(setup)
    if not sim:
        return None
    return copy.deepcopy(sim)
