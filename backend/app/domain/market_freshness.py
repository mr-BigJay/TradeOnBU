from __future__ import annotations

from app.domain.candle_utils import candle_close_ms, interval_ms
from app.domain.candles import Candle


def max_stale_age_seconds(interval: str, configured_stale_seconds: int) -> int:
    """Timeframe-aware staleness: allow a full bar interval after the last close."""
    bar_sec = interval_ms(interval) // 1000
    return max(configured_stale_seconds, bar_sec + 30)


def age_seconds_since_close(last_closed: Candle, interval: str, now_ms: int) -> int:
    close_ms = candle_close_ms(last_closed, interval)
    return max(0, (now_ms - close_ms) // 1000)


def is_market_data_fresh(
    last_closed: Candle,
    interval: str,
    now_ms: int,
    configured_stale_seconds: int,
) -> tuple[bool, int, int]:
    """
    Returns (fresh, age_since_close_sec, allowed_age_sec).
    Uses candle CLOSE time, not open time_ms.
    """
    age = age_seconds_since_close(last_closed, interval, now_ms)
    allowed = max_stale_age_seconds(interval, configured_stale_seconds)
    return age <= allowed, age, allowed
