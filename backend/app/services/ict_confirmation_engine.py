from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.domain.candles import Candle


@dataclass
class ConfirmationProgress:
    sequence: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    confirmation_timestamps: dict[str, int] = field(default_factory=dict)
    confirmation_evidence: dict[str, dict[str, float]] = field(default_factory=dict)
    invalidated: bool = False
    invalidation_reason: str | None = None
    last_bar_ms: int = 0


def _swing_low(candles: list[Candle], lookback: int = 12) -> float | None:
    if len(candles) < 3:
        return None
    window = candles[-lookback:]
    return min(c.low for c in window)


def _swing_high(candles: list[Candle], lookback: int = 12) -> float | None:
    if len(candles) < 3:
        return None
    window = candles[-lookback:]
    return max(c.high for c in window)


def _bar_in_zone(bar: Candle, lo: float, hi: float) -> bool:
    return bar.low <= hi and bar.high >= lo


def _liquidity_sweep_long(candles: list[Candle], lookback: int = 12) -> bool:
    if len(candles) < lookback + 1:
        return False
    prior = candles[:-1]
    bar = candles[-1]
    swing = _swing_low(prior, lookback)
    if swing is None:
        return False
    return bar.low < swing and bar.close > swing


def _liquidity_sweep_short(candles: list[Candle], lookback: int = 12) -> bool:
    if len(candles) < lookback + 1:
        return False
    prior = candles[:-1]
    bar = candles[-1]
    swing = _swing_high(prior, lookback)
    if swing is None:
        return False
    return bar.high > swing and bar.close < swing


def _mss_long(candles: list[Candle], lookback: int = 12) -> bool:
    if len(candles) < lookback + 1:
        return False
    swing_high = _swing_high(candles[:-1], lookback)
    if swing_high is None:
        return False
    return candles[-1].close > swing_high


def _mss_short(candles: list[Candle], lookback: int = 12) -> bool:
    if len(candles) < lookback + 1:
        return False
    swing_low = _swing_low(candles[:-1], lookback)
    if swing_low is None:
        return False
    return candles[-1].close < swing_low


def _bos_long(candles: list[Candle], lookback: int = 8) -> bool:
    return _mss_long(candles, lookback)


def _bos_short(candles: list[Candle], lookback: int = 8) -> bool:
    return _mss_short(candles, lookback)


def _displacement_long(bar: Candle, avg_range: float) -> bool:
    body = abs(bar.close - bar.open)
    return bar.close > bar.open and body >= avg_range * 1.2


def _displacement_short(bar: Candle, avg_range: float) -> bool:
    body = abs(bar.close - bar.open)
    return bar.close < bar.open and body >= avg_range * 1.2


def _avg_range(candles: list[Candle], n: int = 10) -> float:
    if not candles:
        return 0.0
    window = candles[-n:]
    return sum(c.high - c.low for c in window) / len(window)


def _fvg_retest_from_snapshot(bar: Candle, structured: dict[str, Any]) -> bool:
    overlays = structured.get("chart_overlays") or {}
    zones = overlays.get("fvg_zones") or structured.get("ict_fvg") or []
    if not isinstance(zones, list):
        return False
    for z in zones:
        if not isinstance(z, dict):
            continue
        lo = z.get("low") or z.get("bottom")
        hi = z.get("high") or z.get("top")
        try:
            lo_f, hi_f = float(lo), float(hi)
        except (TypeError, ValueError):
            continue
        if lo_f > hi_f:
            lo_f, hi_f = hi_f, lo_f
        if _bar_in_zone(bar, lo_f, hi_f):
            return True
    return False


def _token_on_bar(
    token: str,
    *,
    direction: str,
    window: list[Candle],
    zone_lo: float,
    zone_hi: float,
    structured: dict[str, Any],
) -> bool:
    if not window:
        return False
    bar = window[-1]
    d = direction.upper()
    if token == "entry_zone_touch":
        return _bar_in_zone(bar, zone_lo, zone_hi)
    if token == "liquidity_sweep":
        return _liquidity_sweep_long(window) if d == "LONG" else _liquidity_sweep_short(window)
    if token == "mss":
        return _mss_long(window) if d == "LONG" else _mss_short(window)
    if token == "bos":
        return _bos_long(window) if d == "LONG" else _bos_short(window)
    if token == "displacement":
        avg = _avg_range(window)
        return _displacement_long(bar, avg) if d == "LONG" else _displacement_short(bar, avg)
    if token == "fvg_retest":
        return _fvg_retest_from_snapshot(bar, structured)
    if token == "order_block_retest":
        return _bar_in_zone(bar, zone_lo, zone_hi)
    return False


def _check_invalidation_on_bar(
    bar: Candle,
    *,
    direction: str,
    invalidation_level: float | None,
) -> bool:
    if invalidation_level is None:
        return False
    d = direction.upper()
    if d == "LONG" and bar.close < invalidation_level:
        return True
    if d == "SHORT" and bar.close > invalidation_level:
        return True
    return False


def next_sequence_token(sequence: list[str], completed: list[str]) -> str | None:
    for tok in sequence:
        if tok not in completed:
            return tok
    return None


def sequence_order_valid(sequence: list[str], timestamps: dict[str, int]) -> bool:
    last_ms = -1
    for tok in sequence:
        if tok not in timestamps:
            return False
        ms = timestamps[tok]
        if ms < last_ms:
            return False
        last_ms = ms
    return True


def process_bar_sequential(
    *,
    direction: str,
    sequence: list[str],
    confirmation_state: dict[str, Any],
    bar: Candle,
    window: list[Candle],
    zone_lo: float,
    zone_hi: float,
    invalidation_level: float | None,
    structured: dict[str, Any],
    monitor_from_ms: int,
) -> ConfirmationProgress:
    """Evaluate one closed bar; confirmations must occur in order after monitor_from_ms."""
    timestamps = dict(confirmation_state.get("confirmation_timestamps") or {})
    evidence = dict(confirmation_state.get("confirmation_evidence") or {})
    completed = [t for t in sequence if t in timestamps]
    progress = ConfirmationProgress(
        sequence=list(sequence),
        completed=completed,
        missing=[t for t in sequence if t not in completed],
        confirmation_timestamps=timestamps,
        confirmation_evidence=evidence,
        last_bar_ms=bar.time_ms,
    )

    if bar.time_ms < monitor_from_ms:
        return progress

    if _check_invalidation_on_bar(bar, direction=direction, invalidation_level=invalidation_level):
        progress.invalidated = True
        progress.invalidation_reason = "structural_invalidation_level_breached"
        return progress

    nxt = next_sequence_token(sequence, completed)
    if nxt is None:
        progress.completed = list(sequence)
        progress.missing = []
        return progress

    if _token_on_bar(
        nxt,
        direction=direction,
        window=window,
        zone_lo=zone_lo,
        zone_hi=zone_hi,
        structured=structured,
    ):
        timestamps[nxt] = bar.time_ms
        if nxt == "entry_zone_touch":
            evidence[nxt] = {
                "bar_low": float(bar.low),
                "bar_high": float(bar.high),
                "zone_lo": float(zone_lo),
                "zone_hi": float(zone_hi),
            }
        completed = [t for t in sequence if t in timestamps]
        progress.confirmation_timestamps = timestamps
        progress.confirmation_evidence = evidence
        progress.completed = completed
        progress.missing = [t for t in sequence if t not in completed]

    return progress


def all_required_met(progress: ConfirmationProgress) -> bool:
    if progress.invalidated:
        return False
    if progress.missing:
        return False
    if not progress.sequence:
        return False
    return sequence_order_valid(progress.sequence, progress.confirmation_timestamps)
