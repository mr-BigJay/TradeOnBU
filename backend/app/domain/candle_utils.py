from __future__ import annotations

from app.domain.candles import Candle

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
}


def interval_ms(interval: str) -> int:
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval: {interval}")
    return INTERVAL_MS[interval]


def candle_close_ms(candle: Candle, interval: str) -> int:
    return candle.time_ms + interval_ms(interval)


def is_candle_closed(candle: Candle, interval: str, now_ms: int) -> bool:
    return now_ms >= candle_close_ms(candle, interval)


def split_closed_and_forming(candles: list[Candle], interval: str, now_ms: int) -> tuple[list[Candle], Candle | None]:
    if not candles:
        return [], None
    closed: list[Candle] = []
    forming: Candle | None = None
    for c in candles:
        if is_candle_closed(c, interval, now_ms):
            closed.append(c)
        else:
            forming = c
    return closed, forming


def detect_gaps(candles: list[Candle], interval: str) -> list[int]:
    if len(candles) < 2:
        return []
    step = interval_ms(interval)
    gaps: list[int] = []
    for prev, cur in zip(candles, candles[1:]):
        expected = prev.time_ms + step
        if cur.time_ms > expected:
            gaps.append(expected)
    return gaps
