from __future__ import annotations

from app.domain.candles import Candle

INTERVAL_1M_MS = 60_000


def next_expected_bar_open_ms(cursor_ms: int, interval_ms: int = INTERVAL_1M_MS) -> int:
    """Open time of the next 1m bar after ``cursor_ms`` (last processed bar open)."""
    if cursor_ms <= 0:
        return 0
    return cursor_ms + interval_ms


def leading_history_gap(cursor_ms: int, first_eligible_open_ms: int, interval_ms: int = INTERVAL_1M_MS) -> bool:
    """
    True when at least one whole candle is missing between cursor and the first bar we could process.

    cursor 12:00 + first 12:01 → False (contiguous).
    cursor 12:00 + first 12:02 → True (12:01 missing).
    """
    if cursor_ms <= 0 or first_eligible_open_ms <= 0:
        return False
    expected = next_expected_bar_open_ms(cursor_ms, interval_ms)
    return first_eligible_open_ms > expected


def missing_interval_before_bar(
    prev_processed_open_ms: int, bar_open_ms: int, interval_ms: int = INTERVAL_1M_MS
) -> int | None:
    """Return open ms of missing candle if ``bar`` is not the immediate next bar after ``prev``."""
    if prev_processed_open_ms <= 0:
        return None
    expected = next_expected_bar_open_ms(prev_processed_open_ms, interval_ms)
    if bar_open_ms > expected:
        return expected
    return None


def eligible_bars_after_cursor(
    closed: list[Candle], cursor_ms: int, monitor_from_ms: int
) -> list[Candle]:
    bars = [b for b in closed if b.time_ms > cursor_ms and b.time_ms >= monitor_from_ms]
    return sorted(bars, key=lambda b: b.time_ms)


def chain_contiguous_from_cursor(
    cursor_ms: int, bars: list[Candle], interval_ms: int = INTERVAL_1M_MS
) -> bool:
    if not bars:
        return False
    if cursor_ms > 0 and leading_history_gap(cursor_ms, bars[0].time_ms, interval_ms):
        return False
    prev = cursor_ms
    for bar in bars:
        if prev > 0 and bar.time_ms > prev + interval_ms:
            return False
        prev = bar.time_ms
    return True


def gaps_remaining_block_processing(
    gaps_remaining: list[int],
    cursor_ms: int,
    through_open_ms: int,
    interval_ms: int = INTERVAL_1M_MS,
) -> bool:
    """True if any reported gap start falls in (cursor, through] processing window."""
    if not gaps_remaining:
        return False
    window_start = next_expected_bar_open_ms(cursor_ms, interval_ms) if cursor_ms > 0 else 0
    for gap_start in gaps_remaining:
        if window_start <= gap_start <= through_open_ms:
            return True
    return False


def can_resume_from_suspension(
    cursor_ms: int,
    closed: list[Candle],
    monitor_from_ms: int,
    gaps_remaining: list[int],
    interval_ms: int = INTERVAL_1M_MS,
) -> bool:
    bars = eligible_bars_after_cursor(closed, cursor_ms, monitor_from_ms)
    if not bars:
        return False
    if not chain_contiguous_from_cursor(cursor_ms, bars, interval_ms):
        return False
    if gaps_remaining_block_processing(gaps_remaining, cursor_ms, bars[-1].time_ms, interval_ms):
        return False
    return True
