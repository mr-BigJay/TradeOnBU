"""Closed-5m evidence state machine for AI conditional ideas (paper only).

No model calls, no backdating and no market orders. All trigger timestamps are
from CLOSED bars AFTER the AI plan was created. Earlier bars are references only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.candles import Candle

STEP_MS = 300_000
REFERENCE_BARS = 8
DISPLACEMENT_RATIO = 0.55


@dataclass
class WatchResult:
    evidence: dict[str, Any]
    cursor_ms: int
    state: str  # watching | ready | invalidated
    reason: str


def watch_closed_bars(
    candles: list[Candle],
    *,
    direction: str,
    zone_low: float,
    zone_high: float,
    stop_loss: float,
    take_profit: float,
    start_ms: int,
    cursor_ms: int,
    evidence: dict[str, Any] | None,
    now_ms: int,
    expires_ms: int,
    already_ready: bool = False,
) -> WatchResult:
    """Advance only over a contiguous series of fully closed post-plan bars.

    `start_ms` is the first 5m bar open strictly AFTER plan creation.
    An unresolved missing bar suspends advancement instead of falsely confirming.
    """
    state = "ready" if already_ready else "watching"
    seen = dict(evidence or {})
    cursor = cursor_ms or 0
    expected = cursor + STEP_MS if cursor else start_ms
    ordered = sorted({c.time_ms: c for c in candles}.values(), key=lambda c: c.time_ms)
    reason = "waiting_closed_candle"

    for bar in ordered:
        if bar.time_ms < expected:
            continue
        if bar.time_ms + STEP_MS > now_ms or bar.time_ms + STEP_MS > expires_ms:
            break
        if bar.time_ms != expected:
            return WatchResult(seen, cursor, state, "history_gap")
        prior = [c for c in ordered if c.time_ms < bar.time_ms][-REFERENCE_BARS:]
        if len(prior) < REFERENCE_BARS:
            return WatchResult(seen, cursor, state, "insufficient_reference_bars")
        if (direction == "LONG" and (bar.low <= stop_loss or bar.high >= take_profit)) or (
            direction == "SHORT" and (bar.high >= stop_loss or bar.low <= take_profit)
        ):
            return WatchResult(seen, bar.time_ms, "invalidated", "sl_or_target_touched_before_order")

        if state != "ready":
            # A one-hour-old sweep cannot validate a fresh intraday entry.
            if seen.get("sweep_ms") is not None and bar.time_ms - seen["sweep_ms"] > 12 * STEP_MS:
                seen.pop("sweep_ms", None)
                seen.pop("sweep_level", None)
                seen.pop("sweep_extreme", None)
                seen.pop("poi_ms", None)
            if seen.get("poi_ms") is not None and bar.time_ms - seen["poi_ms"] > 6 * STEP_MS:
                seen.pop("poi_ms", None)
            ref_low = min(c.low for c in prior)
            ref_high = max(c.high for c in prior)
            swept = (
                bar.low < ref_low and bar.close > ref_low
                if direction == "LONG"
                else bar.high > ref_high and bar.close < ref_high
            )
            if swept:
                seen["sweep_ms"] = bar.time_ms
                seen["sweep_level"] = ref_low if direction == "LONG" else ref_high
                seen["sweep_extreme"] = bar.low if direction == "LONG" else bar.high
                seen.pop("poi_ms", None)

            overlaps = bar.low <= zone_high and bar.high >= zone_low
            rejection = (
                bar.close > bar.open and bar.close >= zone_low
                if direction == "LONG"
                else bar.close < bar.open and bar.close <= zone_high
            )
            if seen.get("sweep_ms") is not None and overlaps and rejection:
                seen["poi_ms"] = bar.time_ms

            sweep_ms = seen.get("sweep_ms")
            poi_ms = seen.get("poi_ms")
            body_ratio = abs(bar.close - bar.open) / (bar.high - bar.low) if bar.high > bar.low else 0
            direction_break = (
                bar.close > max(c.high for c in prior[-4:]) and bar.close > bar.open
                if direction == "LONG"
                else bar.close < min(c.low for c in prior[-4:]) and bar.close < bar.open
            )
            if (
                sweep_ms is not None and poi_ms is not None
                and bar.time_ms > sweep_ms and bar.time_ms > poi_ms
                and direction_break and body_ratio >= DISPLACEMENT_RATIO
            ):
                seen["structure_ms"] = bar.time_ms
                seen["structure_break_level"] = (
                    max(c.high for c in prior[-4:])
                    if direction == "LONG"
                    else min(c.low for c in prior[-4:])
                )
                seen["displacement_body_ratio"] = round(body_ratio, 4)
                state = "ready"
                reason = "structure_confirmed"
            elif sweep_ms is None:
                reason = "waiting_liquidity_sweep"
            elif poi_ms is None:
                reason = "waiting_poi_reaction"
            else:
                reason = "waiting_structure_break"
        else:
            reason = "structure_confirmed"

        cursor = bar.time_ms
        expected += STEP_MS

    return WatchResult(seen, cursor, state, reason)
