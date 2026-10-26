"""Read-only hindsight review of recent patterns; NEVER confirmation evidence."""
from app.services.entry_strategies import B, C, strategy_id
from app.services.conditional_setup import parse_conditional_plan
from app.services.confirmation_replay import replay_confirmations_on_5m_bars


def review_recent_patterns(structured, closed_5m):
    results = {}
    extras = structured.get("strategy_candidates")
    extras = extras if isinstance(extras, dict) else {}
    bars = sorted(closed_5m, key=lambda b: b.time_ms)
    if not bars:
        return {"mode": "hindsight_only_not_execution_evidence", "strategies": results}
    boundary = bars[-1].time_ms - 11 * 300000
    recent = [b for b in bars if b.time_ms >= boundary]
    complete = len(recent) == 12 and all(b.time_ms-a.time_ms == 300000 for a,b in zip(recent,recent[1:]))
    for sid in (B, C):
        data = structured if strategy_id(structured) == sid else extras.get(sid)
        if not isinstance(data, dict) or data.get("strategy_id") != sid:
            continue
        try:
            plan, err = parse_conditional_plan(data)
            if err or not plan:
                continue
            if not complete:
                results[sid] = {"available": False, "reason": "incomplete_recent_history"}
                continue
            progress = replay_confirmations_on_5m_bars(
                direction=plan["potential_direction"], sequence=plan["confirmation_sequence"],
                closed_bars=bars[-60:], zone_lo=plan["entry_zone_low"], zone_hi=plan["entry_zone_high"],
                invalidation_level=plan["structural_invalidation_level"], structured=data,
                monitor_from_ms=boundary,
            )
            results[sid] = {"available": True, "lookback_minutes": 60,
                            "completed": progress.completed, "missing": progress.missing,
                            "invalidated": progress.invalidated}
        except (ValueError, TypeError, KeyError, OverflowError):
            results[sid] = {"available": False, "reason": "invalid_candidate_payload"}
    return {"mode": "hindsight_only_not_execution_evidence",
            "warning": "Levels selected after these bars. This is not proof of a missed tradable signal or a backtest.",
            "strategies": results}
