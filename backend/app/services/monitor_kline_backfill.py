"""Compute safe 5m kline fetch depth when monitor cursor lags behind the default window."""

from __future__ import annotations

from app.domain.candle_utils import interval_ms

DEFAULT_MONITOR_KLINE_LIMIT = 120
MAX_MONITOR_KLINE_LIMIT = 500
MIN_MONITOR_KLINE_LIMIT = 120


def next_closed_bar_open_ms(*, cursor_ms: int, boundary_ms: int, interval: str = "5m") -> int:
    """Next 5m bar open on the exchange grid after cursor, not before monitor boundary."""
    step = interval_ms(interval)
    if cursor_ms < boundary_ms:
        return boundary_ms
    return ((cursor_ms // step) + 1) * step


def monitor_kline_limit_for_setups(
    *,
    monitor_cursor_ms: int,
    monitor_from_ms: int,
    now_ms: int,
    interval: str = "5m",
) -> tuple[int, dict]:
    """
    Return (limit, meta) for fetch_kline_bundle so no eligible closed bar is silently skipped.
    """
    step = interval_ms(interval)
    meta: dict = {
        "default_limit": DEFAULT_MONITOR_KLINE_LIMIT,
        "cursor_ms": monitor_cursor_ms,
        "boundary_ms": monitor_from_ms,
        "now_ms": now_ms,
    }
    if monitor_cursor_ms < 0:
        return DEFAULT_MONITOR_KLINE_LIMIT, {**meta, "reason": "cursor_uninitialized"}

    lag_bars = max(0, (now_ms - monitor_cursor_ms) // step)
    # Bars from boundary to now (inclusive buffer for close detection)
    span_from_boundary = max(0, (now_ms - monitor_from_ms) // step + 5)
    needed = max(MIN_MONITOR_KLINE_LIMIT, lag_bars + 15, span_from_boundary)
    limit = min(MAX_MONITOR_KLINE_LIMIT, needed)
    meta.update(
        {
            "lag_bars_estimate": lag_bars,
            "span_from_boundary": span_from_boundary,
            "chosen_limit": limit,
            "backfill": limit > DEFAULT_MONITOR_KLINE_LIMIT,
        }
    )
    return limit, meta


def aggregate_monitor_kline_limit(setups: list, *, now_ms: int, interval: str = "5m") -> tuple[int, list[dict]]:
    """Worst-case limit across active setups."""
    if not setups:
        return DEFAULT_MONITOR_KLINE_LIMIT, []
    per_setup: list[dict] = []
    chosen = DEFAULT_MONITOR_KLINE_LIMIT
    for setup in setups:
        boundary = setup.monitor_from_ms or 0
        cursor = setup.monitor_cursor_ms if setup.monitor_cursor_ms is not None else -1
        lim, meta = monitor_kline_limit_for_setups(
            monitor_cursor_ms=cursor,
            monitor_from_ms=boundary,
            now_ms=now_ms,
            interval=interval,
        )
        meta["setup_id"] = setup.setup_id
        per_setup.append(meta)
        chosen = max(chosen, lim)
    return chosen, per_setup


def detect_skipped_closed_bars(
    *,
    closed_bars: list,
    cursor_ms: int,
    boundary_ms: int,
    now_ms: int,
    interval: str = "5m",
) -> list[int]:
    """Closed bar open times missing inside the fetched closed window after cursor (gap risk)."""
    if not closed_bars:
        return []
    step = interval_ms(interval)
    from app.domain.candle_utils import candle_close_ms

    eligible = [b for b in closed_bars if now_ms >= candle_close_ms(b, interval)]
    if not eligible:
        return []
    present = {b.time_ms for b in eligible}
    # Only infer gaps within data we actually fetched — not for bars beyond the bundle.
    upper = max(present)

    expected: list[int] = []
    t = next_closed_bar_open_ms(cursor_ms=cursor_ms, boundary_ms=boundary_ms, interval=interval)
    while t <= upper:
        expected.append(t)
        t += step
    return [open_ms for open_ms in expected if open_ms not in present]


def first_discontinuity_open_ms(
    *,
    new_bars: list,
    cursor_ms: int,
    boundary_ms: int,
    interval: str = "5m",
) -> int | None:
    """First expected 5m open missing within `new_bars` after cursor (grid-aware, boundary-safe)."""
    if not new_bars:
        return None
    step = interval_ms(interval)
    sorted_bars = sorted(new_bars, key=lambda c: c.time_ms)
    t = next_closed_bar_open_ms(cursor_ms=cursor_ms, boundary_ms=boundary_ms, interval=interval)
    i = 0
    while t <= sorted_bars[-1].time_ms:
        while i < len(sorted_bars) and sorted_bars[i].time_ms < t:
            i += 1
        if i >= len(sorted_bars) or sorted_bars[i].time_ms > t:
            return t
        i += 1
        t += step
    return None
