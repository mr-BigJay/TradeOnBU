"""Persist independent candidates from one inference; never bypass entry gates."""
from copy import deepcopy

from app.services.entry_strategies import LABELS, strategy_id, strategy_settings
from app.services.ai_setup_memory import create_setup_from_structured


async def persist_strategy_candidates(session, *, run_id, symbol, structured, market_intelligence=None):
    enabled = await strategy_settings(session)
    primary = strategy_id(structured)
    extras = structured.get("strategy_candidates", {})
    results, rows = {}, {}
    for sid in LABELS:
        base = {"created": False, "updated": False, "skipped": True}
        if not enabled[sid]:
            results[sid] = {**base, "reason": "entry_strategy_disabled"}
            continue
        if sid == primary:
            candidate = structured
        elif not isinstance(extras, dict):
            results[sid] = {**base, "reason": "invalid_candidates_object"}
            continue
        elif sid not in extras:
            results[sid] = {**base, "reason": "candidate_not_reported"}
            continue
        else:
            candidate = extras[sid]
        if candidate is None:
            results[sid] = {**base, "reason": "no_candidate"}
            continue
        if not isinstance(candidate, dict) or candidate.get("strategy_id", primary if sid == primary else None) != sid:
            results[sid] = {**base, "reason": "candidate_strategy_mismatch"}
            continue
        data = deepcopy(candidate)
        for key in ("strategy_candidates", "_artifacts", "report_fa", "chart_overlays"):
            data.pop(key, None)
        try:
            # Malformed candidate cannot roll back its siblings. Database and
            # programming errors deliberately propagate rather than hiding failures.
            async with session.begin_nested():
                row, meta = await create_setup_from_structured(
                    session, run_id=run_id, symbol=symbol, structured=data,
                    market_intelligence=market_intelligence,
                )
        except (ValueError, TypeError, KeyError, OverflowError):
            row, meta = None, {**base, "reason": "invalid_candidate_payload"}
        results[sid] = meta
        if row is not None:
            rows[sid] = row
            results[sid] = {**meta, "setup_id": row.setup_id, "lifecycle_state": row.lifecycle_state}
    return rows, results
