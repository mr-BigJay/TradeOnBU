"""STRICT ↔ BALANCED transitions without incompatible confirmation carry-over."""

from __future__ import annotations

from typing import Any

from app.domain.entry_execution import ENTRY_MODE_BALANCED, ENTRY_MODE_STRICT, normalize_entry_mode
def _active_engine(notes: dict[str, Any], conf: dict[str, Any]) -> str | None:
    eng = conf.get("confirmation_engine") or notes.get("entry_execution_engine_active")
    if isinstance(eng, str):
        return normalize_entry_mode(eng) if eng in (ENTRY_MODE_STRICT, ENTRY_MODE_BALANCED) else None
    return None


def reconcile_setup_entry_engine(
    setup: Any,
    *,
    live_engine: str,
) -> bool:
    """
    Align persisted confirmation state with live engine after settings change or restart.
    Returns True if state was modified.
    """
    live_engine = normalize_entry_mode(live_engine)
    notes = dict(setup.objective_notes or {})
    conf = dict(setup.confirmation_state or {})
    prior = _active_engine(notes, conf)
    if prior is None:
        notes["entry_execution_engine_active"] = live_engine
        setup.objective_notes = notes
        conf["confirmation_engine"] = live_engine
        setup.confirmation_state = conf
        return True

    if prior == live_engine:
        return False

    sequence = list(conf.get("sequence") or conf.get("required") or [])
    timestamps = dict(conf.get("confirmation_timestamps") or {})
    evidence = dict(conf.get("confirmation_evidence") or {})

    if prior == ENTRY_MODE_BALANCED and live_engine == ENTRY_MODE_STRICT:
        # Drop BALANCED-only 1m stamps; STRICT must re-derive on closed 5m.
        first_drop: int | None = None
        for i, tok in enumerate(sequence):
            ev = evidence.get(tok) or {}
            if ev.get("engine") == "BALANCED" or ev.get("granularity") == "1m":
                first_drop = i
                break
        if first_drop is not None:
            for tok in sequence[first_drop:]:
                timestamps.pop(tok, None)
                evidence.pop(tok, None)
            conf["confirmation_timestamps"] = timestamps
            conf["confirmation_evidence"] = evidence
            conf["completed"] = [t for t in sequence if t in timestamps]
            conf["missing"] = [t for t in sequence if t not in timestamps]
            conf["monitor_1m_cursor_ms"] = int(conf.get("monitor_from_ms") or setup.monitor_from_ms or 0) - 1

    notes["entry_execution_engine_active"] = live_engine
    notes["entry_execution_engine_switched_from"] = prior
    notes["entry_execution_engine_switched_to"] = live_engine
    conf["confirmation_engine"] = live_engine
    setup.objective_notes = notes
    setup.confirmation_state = conf
    return True
