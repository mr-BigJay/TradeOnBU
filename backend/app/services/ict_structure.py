from __future__ import annotations

from dataclasses import dataclass

from app.domain.candles import Candle
from app.models import TrendState

FVG_MAX_AGE_BARS = 12
DISPLACEMENT_BODY_RATIO = 0.55


@dataclass
class SwingPoint:
    index: int
    price: float
    kind: str  # HIGH or LOW
    time_ms: int


@dataclass
class FVGZone:
    low: float
    high: float
    direction: str
    index: int
    time_ms: int


@dataclass
class StructureAnalysis:
    trend: TrendState
    swings: list[SwingPoint]
    last_bos: str | None
    last_mss: str | None
    liquidity_levels: list[float]
    has_clear_structure: bool
    summary: str


@dataclass
class ICTSequenceResult:
    ok: bool
    reason: str | None
    sweep_index: int | None
    mss_index: int | None
    displacement_index: int | None
    fvg: FVGZone | None
    liquidity_level: float | None
    liquidity_sweep: bool
    mss_confirmation: bool
    displacement: bool
    fvg_valid: bool


def confirmed_candles(candles: list[Candle]) -> list[Candle]:
    return candles[:-1] if len(candles) > 1 else list(candles)


def find_swings(candles: list[Candle], left: int = 2, right: int = 2) -> list[SwingPoint]:
    swings: list[SwingPoint] = []
    if len(candles) < left + right + 1:
        return swings
    for i in range(left, len(candles) - right):
        window = candles[i - left : i + right + 1]
        highs = [c.high for c in window]
        lows = [c.low for c in window]
        if candles[i].high == max(highs):
            swings.append(SwingPoint(index=i, price=candles[i].high, kind="HIGH", time_ms=candles[i].time_ms))
        if candles[i].low == min(lows):
            swings.append(SwingPoint(index=i, price=candles[i].low, kind="LOW", time_ms=candles[i].time_ms))
    return swings


def detect_equal_liquidity(swings: list[SwingPoint], tolerance_pct: float = 0.05) -> list[float]:
    levels: list[float] = []
    highs = [s.price for s in swings if s.kind == "HIGH"]
    lows = [s.price for s in swings if s.kind == "LOW"]
    for group in (highs, lows):
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                mid = (a + b) / 2
                if mid == 0:
                    continue
                if abs(a - b) / mid * 100 <= tolerance_pct:
                    levels.append(mid)
    return sorted(set(round(l, 2) for l in levels))


def analyze_structure(candles: list[Candle]) -> StructureAnalysis:
    confirmed = confirmed_candles(candles)
    if len(confirmed) < 30:
        return StructureAnalysis(
            trend=TrendState.UNCERTAIN,
            swings=[],
            last_bos=None,
            last_mss=None,
            liquidity_levels=[],
            has_clear_structure=False,
            summary="Insufficient confirmed candles",
        )

    swings = find_swings(confirmed)
    if len(swings) < 4:
        return StructureAnalysis(
            trend=TrendState.UNCERTAIN,
            swings=swings,
            last_bos=None,
            last_mss=None,
            liquidity_levels=detect_equal_liquidity(swings),
            has_clear_structure=False,
            summary="Not enough swing points",
        )

    highs = [s for s in swings if s.kind == "HIGH"]
    lows = [s for s in swings if s.kind == "LOW"]
    last_close = confirmed[-1].close

    hh = len(highs) >= 2 and highs[-1].price > highs[-2].price
    hl = len(lows) >= 2 and lows[-1].price > lows[-2].price
    lh = len(highs) >= 2 and highs[-1].price < highs[-2].price
    ll = len(lows) >= 2 and lows[-1].price < lows[-2].price

    trend = TrendState.SIDEWAYS
    if hh and hl:
        trend = TrendState.BULLISH
    elif lh and ll:
        trend = TrendState.BEARISH
    elif not (hh or hl or lh or ll):
        trend = TrendState.UNCERTAIN

    last_bos: str | None = None
    last_mss: str | None = None
    if highs and lows:
        last_high = highs[-1].price
        last_low = lows[-1].price
        if trend == TrendState.BULLISH and last_close > last_high:
            last_bos = "BULLISH_BOS"
        elif trend == TrendState.BEARISH and last_close < last_low:
            last_bos = "BEARISH_BOS"
        if trend == TrendState.BULLISH and last_close < last_low:
            last_mss = "BEARISH_MSS"
        if trend == TrendState.BEARISH and last_close > last_high:
            last_mss = "BULLISH_MSS"

    liquidity = detect_equal_liquidity(swings)
    clear = trend in (TrendState.BULLISH, TrendState.BEARISH) and len(swings) >= 4

    return StructureAnalysis(
        trend=trend,
        swings=swings,
        last_bos=last_bos,
        last_mss=last_mss,
        liquidity_levels=liquidity,
        has_clear_structure=clear,
        summary=f"Trend={trend.value}, BOS={last_bos}, MSS={last_mss}",
    )


def find_fvg_zones(candles: list[Candle]) -> list[FVGZone]:
    zones: list[FVGZone] = []
    confirmed = confirmed_candles(candles)
    for i in range(2, len(confirmed)):
        c0, c2 = confirmed[i - 2], confirmed[i]
        if c2.low > c0.high:
            zones.append(FVGZone(low=c0.high, high=c2.low, direction="LONG", index=i, time_ms=c2.time_ms))
        if c0.low > c2.high:
            zones.append(FVGZone(low=c2.high, high=c0.low, direction="SHORT", index=i, time_ms=c2.time_ms))
    return zones


def fvg_is_mitigated(candles: list[Candle], fvg: FVGZone) -> bool:
    confirmed = confirmed_candles(candles)
    for c in confirmed[fvg.index + 1 :]:
        if fvg.direction == "LONG" and c.low <= fvg.high:
            return True
        if fvg.direction == "SHORT" and c.high >= fvg.low:
            return True
    return False


def fvg_is_fresh(candles: list[Candle], fvg: FVGZone, max_age_bars: int = FVG_MAX_AGE_BARS) -> bool:
    confirmed = confirmed_candles(candles)
    age = len(confirmed) - 1 - fvg.index
    return 0 <= age <= max_age_bars


def detect_displacement_at(candles: list[Candle], index: int, direction: str) -> bool:
    confirmed = confirmed_candles(candles)
    if index < 0 or index >= len(confirmed):
        return False
    c = confirmed[index]
    rng = c.high - c.low
    if rng <= 0:
        return False
    body = abs(c.close - c.open)
    if body / rng < DISPLACEMENT_BODY_RATIO:
        return False
    if direction == "LONG":
        return c.close > c.open
    return c.close < c.open


def find_liquidity_sweep(candles: list[Candle], level: float, direction: str) -> int | None:
    """Return candle index of sweep, or None."""
    confirmed = confirmed_candles(candles)
    for i in range(max(0, len(confirmed) - 20), len(confirmed)):
        c = confirmed[i]
        if direction == "LONG" and c.low < level and c.close > level:
            return i
        if direction == "SHORT" and c.high > level and c.close < level:
            return i
    return None


def validate_ict_sequence(candles: list[Candle], direction: str) -> ICTSequenceResult:
    confirmed = confirmed_candles(candles)
    s5 = analyze_structure(candles)
    if not s5.liquidity_levels:
        return ICTSequenceResult(
            ok=False,
            reason="NO_LIQUIDITY_LEVEL",
            sweep_index=None,
            mss_index=None,
            displacement_index=None,
            fvg=None,
            liquidity_level=None,
            liquidity_sweep=False,
            mss_confirmation=False,
            displacement=False,
            fvg_valid=False,
        )

    level = s5.liquidity_levels[-1]
    sweep_idx = find_liquidity_sweep(candles, level, direction)
    if sweep_idx is None:
        return ICTSequenceResult(
            ok=False,
            reason="NO_VALID_SWEEP",
            sweep_index=None,
            mss_index=None,
            displacement_index=None,
            fvg=None,
            liquidity_level=level,
            liquidity_sweep=False,
            mss_confirmation=False,
            displacement=False,
            fvg_valid=False,
        )

    mss_idx = None
    for i in range(sweep_idx + 1, len(confirmed)):
        partial = analyze_structure(confirmed[: i + 1])
        if direction == "LONG" and partial.last_mss == "BULLISH_MSS":
            mss_idx = i
            break
        if direction == "SHORT" and partial.last_mss == "BEARISH_MSS":
            mss_idx = i
            break

    disp_idx = None
    for i in range(sweep_idx + 1, len(confirmed)):
        if detect_displacement_at(candles, i, direction):
            disp_idx = i
            break

    if mss_idx is None:
        return ICTSequenceResult(
            ok=False,
            reason="NO_MSS",
            sweep_index=sweep_idx,
            mss_index=None,
            displacement_index=disp_idx,
            fvg=None,
            liquidity_level=level,
            liquidity_sweep=True,
            mss_confirmation=False,
            displacement=disp_idx is not None,
            fvg_valid=False,
        )

    if disp_idx is None:
        return ICTSequenceResult(
            ok=False,
            reason="NO_DISPLACEMENT",
            sweep_index=sweep_idx,
            mss_index=mss_idx,
            displacement_index=None,
            fvg=None,
            liquidity_level=level,
            liquidity_sweep=True,
            mss_confirmation=True,
            displacement=False,
            fvg_valid=False,
        )

    pivot = max(mss_idx, disp_idx)
    fvgs = [z for z in find_fvg_zones(candles) if z.direction == direction and z.index >= pivot]
    fvg = fvgs[-1] if fvgs else None
    if not fvg:
        return ICTSequenceResult(
            ok=False,
            reason="NO_FVG_AFTER_SEQUENCE",
            sweep_index=sweep_idx,
            mss_index=mss_idx,
            displacement_index=disp_idx,
            fvg=None,
            liquidity_level=level,
            liquidity_sweep=True,
            mss_confirmation=mss_idx is not None,
            displacement=disp_idx is not None,
            fvg_valid=False,
        )

    if fvg_is_mitigated(candles, fvg):
        return ICTSequenceResult(
            ok=False,
            reason="FVG_MITIGATED",
            sweep_index=sweep_idx,
            mss_index=mss_idx,
            displacement_index=disp_idx,
            fvg=fvg,
            liquidity_level=level,
            liquidity_sweep=True,
            mss_confirmation=mss_idx is not None,
            displacement=disp_idx is not None,
            fvg_valid=False,
        )

    if not fvg_is_fresh(candles, fvg):
        return ICTSequenceResult(
            ok=False,
            reason="FVG_STALE",
            sweep_index=sweep_idx,
            mss_index=mss_idx,
            displacement_index=disp_idx,
            fvg=fvg,
            liquidity_level=level,
            liquidity_sweep=True,
            mss_confirmation=mss_idx is not None,
            displacement=disp_idx is not None,
            fvg_valid=False,
        )

    if not (sweep_idx < pivot < fvg.index):
        return ICTSequenceResult(
            ok=False,
            reason="INVALID_TIME_ORDER",
            sweep_index=sweep_idx,
            mss_index=mss_idx,
            displacement_index=disp_idx,
            fvg=fvg,
            liquidity_level=level,
            liquidity_sweep=True,
            mss_confirmation=mss_idx is not None,
            displacement=disp_idx is not None,
            fvg_valid=False,
        )

    return ICTSequenceResult(
        ok=True,
        reason=None,
        sweep_index=sweep_idx,
        mss_index=mss_idx,
        displacement_index=disp_idx,
        fvg=fvg,
        liquidity_level=level,
        liquidity_sweep=True,
        mss_confirmation=mss_idx is not None,
        displacement=disp_idx is not None,
        fvg_valid=True,
    )


def build_chart_overlays(candles: list[Candle], direction: str | None = None) -> dict:
    confirmed = confirmed_candles(candles)
    s = analyze_structure(candles)
    swings = [{"time_ms": sw.time_ms, "price": sw.price, "kind": sw.kind} for sw in s.swings[-20:]]
    fvgs = [
        {
            "low": z.low,
            "high": z.high,
            "direction": z.direction,
            "time_ms": z.time_ms,
            "mitigated": fvg_is_mitigated(candles, z),
        }
        for z in find_fvg_zones(candles)[-10:]
    ]
    seq = validate_ict_sequence(candles, direction) if direction else None
    sweep_marker = None
    if seq and seq.sweep_index is not None:
        sweep_marker = {"time_ms": confirmed[seq.sweep_index].time_ms, "level": seq.liquidity_level}
    return {
        "swings": swings,
        "bos": s.last_bos,
        "mss": s.last_mss,
        "liquidity_levels": s.liquidity_levels,
        "liquidity_sweep": sweep_marker,
        "fvg_zones": fvgs,
        "sequence_ok": seq.ok if seq else None,
    }
