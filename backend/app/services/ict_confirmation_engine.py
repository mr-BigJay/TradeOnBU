from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.domain.candles import Candle


@dataclass
class ConfirmationProgress:
    completed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
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


def _fvg_retest_from_snapshot(bar: Candle, structured: dict[str, Any], direction: str) -> bool:
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


def _check_token(
    token: str,
    *,
    direction: str,
    candles: list[Candle],
    zone_lo: float,
    zone_hi: float,
    structured: dict[str, Any],
) -> bool:
    bar = candles[-1]
    d = direction.upper()
    if token == "entry_zone_touch":
        return any(_bar_in_zone(c, zone_lo, zone_hi) for c in candles)
    if token == "liquidity_sweep":
        return _liquidity_sweep_long(candles) if d == "LONG" else _liquidity_sweep_short(candles)
    if token == "mss":
        return _mss_long(candles) if d == "LONG" else _mss_short(candles)
    if token == "bos":
        return _bos_long(candles) if d == "LONG" else _bos_short(candles)
    if token == "displacement":
        avg = _avg_range(candles)
        return _displacement_long(bar, avg) if d == "LONG" else _displacement_short(bar, avg)
    if token == "fvg_retest":
        return _fvg_retest_from_snapshot(bar, structured, direction)
    if token == "order_block_retest":
        return _bar_in_zone(bar, zone_lo, zone_hi)
    return False


def evaluate_confirmations(
    *,
    direction: str,
    required: list[str],
    completed_prior: list[str],
    candles: list[Candle],
    zone_lo: float,
    zone_hi: float,
    invalidation_level: float | None,
    structured: dict[str, Any],
) -> ConfirmationProgress:
    """Evaluate objective confirmations on closed candles only (no look-ahead)."""
    progress = ConfirmationProgress(
        completed=list(completed_prior),
        missing=[t for t in required if t not in completed_prior],
    )
    if not candles:
        progress.missing = list(required)
        return progress

    progress.last_bar_ms = candles[-1].time_ms
    d = direction.upper()

    if invalidation_level is not None:
        for bar in candles:
            if d == "LONG" and bar.close < invalidation_level:
                progress.invalidated = True
                progress.invalidation_reason = "structural_invalidation_level_breached"
                return progress
            if d == "SHORT" and bar.close > invalidation_level:
                progress.invalidated = True
                progress.invalidation_reason = "structural_invalidation_level_breached"
                return progress

    completed = set(progress.completed)
    for token in required:
        if token in completed:
            continue
        if _check_token(
            token,
            direction=direction,
            candles=candles,
            zone_lo=zone_lo,
            zone_hi=zone_hi,
            structured=structured,
        ):
            completed.add(token)

    progress.completed = [t for t in required if t in completed]
    progress.missing = [t for t in required if t not in completed]
    return progress


def all_required_met(progress: ConfirmationProgress) -> bool:
    return not progress.missing and not progress.invalidated
