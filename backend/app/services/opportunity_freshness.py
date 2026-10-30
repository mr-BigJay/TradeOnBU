"""Bounded, candle-proven context and single-attempt conditional Paper plans.

History can establish a structural prefix, never a fill or a reaction/touch.
Execution boundaries remain forward-only. No model booleans grant permission.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.domain.candles import Candle

from app.domain.setup_lifecycle import EXPIRED
from app.services.ict_confirmation_engine import _check_invalidation_on_bar, _token_on_bar

STEP = 300_000
LOOKBACK_BARS = 12  # one hour; plus twelve earlier bars for swing context
STRUCTURAL_PREFIX = frozenset({"liquidity_sweep", "mss", "bos", "breakout_close"})


def active(setup: Any) -> bool:
    return (setup.conditional_plan or {}).get("freshness_policy_version") == 1


def retire(setup: Any, reason: str, at_ms: int) -> None:
    setup.status = "expired"
    setup.lifecycle_state = EXPIRED
    setup.monitoring_enabled = False
    setup.closed_at = datetime.fromtimestamp(at_ms / 1000, timezone.utc)
    setup.last_rejection_reason = reason
    setup.last_validation_result = {"ok": False, "reason": reason, "at_ms": at_ms}
    notes = dict(setup.objective_notes or {})
    notes["opportunity_retirement"] = {"reason": reason, "at_ms": at_ms}
    setup.objective_notes = notes


def seed_recent_structure(setup: Any, bars: list[Candle], *, data_complete: bool) -> str | None:
    """One-time replay of a contiguous hour CLOSED at plan creation/revision.

    Failure to obtain history never licenses an order. Retry when coverage returns.
    Only the ordered structural prefix is seeded; reactions must occur forward.
    """
    if not active(setup):
        return None
    notes = dict(setup.objective_notes or {})
    if (notes.get("historical_confirmation_seed") or {}).get("checked"):
        return None
    conf = dict(setup.confirmation_state or {})
    # Do not graft older evidence onto an already-running sequence.
    if conf.get("confirmation_timestamps"):
        notes["historical_confirmation_seed"] = {"checked": True, "reason": "live_sequence_started"}
        setup.objective_notes = notes
        return None
    cutoff = int(setup.conditional_plan["registered_at_ms"])
    end = (cutoff // STEP) * STEP
    start = end - LOOKBACK_BARS * STEP
    by_time = {b.time_ms: b for b in bars if b.time_ms + STEP <= cutoff}
    expected = list(range(start - 12 * STEP, end, STEP))
    if not data_complete or any(t not in by_time for t in expected):
        notes["historical_confirmation_seed"] = {"checked": False, "reason": "history_incomplete"}
        setup.objective_notes = notes
        return None
    seq = list(conf.get("sequence") or conf.get("required") or [])
    prefix = []
    for token in seq:
        if token not in STRUCTURAL_PREFIX:
            break
        prefix.append(token)
    timestamps, evidence = {}, {}
    history = [by_time[t] for t in expected]
    reason = None
    for i, bar in enumerate(history):
        if bar.time_ms < start:
            continue
        if _check_invalidation_on_bar(bar, direction=setup.direction, invalidation_level=setup.invalidation_level):
            # A pre-setup invalidation discards earlier candidates; later structure may be new.
            timestamps, evidence = {}, {}
            continue
        target = (bar.high >= setup.take_profit if setup.direction == "LONG" else bar.low <= setup.take_profit)
        if timestamps and target:
            reason = "target_reached_before_registration"
            break
        if len(timestamps) < len(prefix):
            token = prefix[len(timestamps)]
            if _token_on_bar(token, direction=setup.direction, window=history[max(0, i-47):i+1],
                             zone_lo=setup.entry_zone_low, zone_hi=setup.entry_zone_high,
                             structured=setup.structured_snapshot or {}):
                timestamps[token] = bar.time_ms
                evidence[token] = {"source": "closed_history", "engine": "STRICT", "granularity": "5m",
                                   "bar_5m_ms": bar.time_ms, "verified_at_ms": cutoff,
                                   "ohlc": [bar.open, bar.high, bar.low, bar.close]}
                if target:
                    reason = "target_reached_before_registration"
                    break
                continue  # one token per candle; never infer intrabar order
        if prefix and len(timestamps) == len(prefix):
            # A later reaction has already used this structural opportunity.
            touched = bar.low <= setup.entry_zone_high and bar.high >= setup.entry_zone_low
            if touched:
                reason = "entry_zone_used_before_registration"
                break
    seed = {"sequence": seq, "required": conf.get("required", seq),
            "confirmation_timestamps": timestamps, "confirmation_evidence": evidence}
    notes["historical_confirmation_seed"] = {"checked": True, "from_ms": start, "cutoff_ms": cutoff,
                                              "reason": reason, "snapshot": seed}
    setup.objective_notes = notes
    if reason:
        retire(setup, reason, cutoff)
        return reason
    conf.update(seed)
    conf["completed"] = [t for t in seq if t in timestamps]
    conf["missing"] = [t for t in seq if t not in timestamps]
    setup.confirmation_state = conf
    return None


def consume_unordered_opportunity(setup: Any, bars: list[Candle], *, interval_ms: int) -> str | None:
    """Only call after confirming no pending/open execution exists.

    A first zone visit followed by a favorable departure cannot become a later
    limit fill. Target before an order retires even a never-touched entry.
    """
    if not active(setup):
        return None
    notes = dict(setup.objective_notes or {})
    progress = dict(notes.get("unfilled_opportunity") or {})
    cursor = progress.get("cursor_ms", setup.monitor_from_ms - 1)
    for bar in sorted(bars, key=lambda b: b.time_ms):
        if bar.time_ms < setup.monitor_from_ms or bar.time_ms <= cursor:
            continue
        target = bar.high >= setup.take_profit if setup.direction == "LONG" else bar.low <= setup.take_profit
        if target:
            reason = "target_reached_without_order"
        else:
            if bar.low <= setup.entry_zone_high and bar.high >= setup.entry_zone_low:
                progress.setdefault("first_zone_touch_ms", bar.time_ms)
            departed = bar.close > setup.entry_zone_high if setup.direction == "LONG" else bar.close < setup.entry_zone_low
            reason = "first_touch_missed" if "first_zone_touch_ms" in progress and departed else None
        progress["cursor_ms"] = bar.time_ms
        if reason:
            notes["unfilled_opportunity"] = progress
            setup.objective_notes = notes
            retire(setup, reason, bar.time_ms + interval_ms)
            return reason
    notes["unfilled_opportunity"] = progress
    setup.objective_notes = notes
    return None
