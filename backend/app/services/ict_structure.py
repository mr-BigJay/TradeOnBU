from __future__ import annotations

from dataclasses import dataclass

from app.domain.candles import Candle
from app.models import TrendState


@dataclass
class SwingPoint:
    index: int
    price: float
    kind: str  # HIGH or LOW


@dataclass
class StructureAnalysis:
    trend: TrendState
    swings: list[SwingPoint]
    last_bos: str | None
    last_mss: str | None
    liquidity_levels: list[float]
    has_clear_structure: bool
    summary: str


def find_swings(candles: list[Candle], left: int = 2, right: int = 2) -> list[SwingPoint]:
    swings: list[SwingPoint] = []
    if len(candles) < left + right + 1:
        return swings
    for i in range(left, len(candles) - right):
        window = candles[i - left : i + right + 1]
        highs = [c.high for c in window]
        lows = [c.low for c in window]
        if candles[i].high == max(highs):
            swings.append(SwingPoint(index=i, price=candles[i].high, kind="HIGH"))
        if candles[i].low == min(lows):
            swings.append(SwingPoint(index=i, price=candles[i].low, kind="LOW"))
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
    if len(candles) < 30:
        return StructureAnalysis(
            trend=TrendState.UNCERTAIN,
            swings=[],
            last_bos=None,
            last_mss=None,
            liquidity_levels=[],
            has_clear_structure=False,
            summary="Insufficient confirmed candles",
        )

    confirmed = candles[:-1] if len(candles) > 1 else candles
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


def find_fvg_zones(candles: list[Candle]) -> list[tuple[float, float, str]]:
    """Return list of (low, high, direction) FVG zones from last 50 confirmed candles."""
    zones: list[tuple[float, float, str]] = []
    confirmed = candles[:-1] if len(candles) > 1 else candles
    for i in range(2, len(confirmed)):
        c0, c1, c2 = confirmed[i - 2], confirmed[i - 1], confirmed[i]
        # Bullish FVG: gap between c0 high and c2 low
        if c2.low > c0.high:
            zones.append((c0.high, c2.low, "LONG"))
        if c0.low > c2.high:
            zones.append((c2.high, c0.low, "SHORT"))
    return zones[-5:]


def detect_liquidity_sweep(candles: list[Candle], level: float, direction: str) -> bool:
    if len(candles) < 3:
        return False
    recent = candles[-4:-1]
    if direction == "LONG":
        swept = any(c.low < level and c.close > level for c in recent)
        return swept
    swept = any(c.high > level and c.close < level for c in recent)
    return swept
