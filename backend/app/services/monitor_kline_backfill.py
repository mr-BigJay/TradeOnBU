"""Compute safe 5m kline fetch depth when monitor cursor lags behind the default window."""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.candle_utils import interval_ms
from app.domain.setup_monitor_boundary import interval_grid_first_open_at_or_after

DEFAULT_MONITOR_KLINE_LIMIT = 120
MAX_MONITOR_KLINE_LIMIT = 500
MIN_MONITOR_KLINE_LIMIT = 120
STRUCTURAL_CONTEXT_BARS = 48
CLOSE_BUFFER_BARS = 15

# Paginated historical recovery (Bitunix 200 bars/request × up to 12 pages in market_data).
MONITOR_PAGINATED_MAX_BARS = 2400


@dataclass
class MonitorFetchPlan:
    setup_id: str
    boundary_ms: int
    cursor_ms: int
    lag_bars: int
    bars_needed: int
    from_ms: int
    mode: str  # recent_window | extended_window | paginated
    needs_pagination: bool
    exceeds_pagination_ceiling: bool
    max_bars: int
    single_request_limit: int


def next_closed_bar_open_ms(*, cursor_ms: int, boundary_ms: int, interval: str = "5m") -> int:
    """Next closed bar open on the interval grid after cursor (boundary may be 1m-aligned)."""
    step = interval_ms(interval)
    grid_floor = interval_grid_first_open_at_or_after(boundary_ms, interval)
    after_cursor = ((cursor_ms // step) + 1) * step
    return max(grid_floor, after_cursor)


def live_monitor_fetch_plan(
    *,
    setup_id: str,
    boundary_ms: int,
    cursor_ms: int,
    now_ms: int,
    interval: str = "5m",
) -> MonitorFetchPlan:
    """
    Live monitor history: anchor on cursor + structural context, not full setup lifetime.
    """
    step = interval_ms(interval)
    if boundary_ms % step != 0:
        boundary_ms = interval_grid_first_open_at_or_after(boundary_ms, interval)
    context_ms = STRUCTURAL_CONTEXT_BARS * step
    effective_cursor = cursor_ms if cursor_ms >= 0 else boundary_ms - step
    lag_bars = max(0, (now_ms - effective_cursor) // step)
    bars_needed = lag_bars + STRUCTURAL_CONTEXT_BARS + CLOSE_BUFFER_BARS
    # Cursor-anchored backfill: scenario boundary gates confirmations only, not re-download of processed life.
    if effective_cursor >= boundary_ms:
        from_ms = max(0, effective_cursor - context_ms)
    else:
        from_ms = max(0, boundary_ms - context_ms)

    exceeds = bars_needed > MONITOR_PAGINATED_MAX_BARS
    capped_needed = min(bars_needed, MONITOR_PAGINATED_MAX_BARS)

    if bars_needed <= DEFAULT_MONITOR_KLINE_LIMIT:
        mode = "recent_window"
        needs_pagination = False
        single_limit = max(MIN_MONITOR_KLINE_LIMIT, bars_needed)
        max_bars = single_limit
    elif bars_needed <= MAX_MONITOR_KLINE_LIMIT:
        mode = "extended_window"
        needs_pagination = False
        single_limit = capped_needed
        max_bars = single_limit
    else:
        mode = "paginated"
        needs_pagination = True
        single_limit = MAX_MONITOR_KLINE_LIMIT
        max_bars = capped_needed

    return MonitorFetchPlan(
        setup_id=setup_id,
        boundary_ms=boundary_ms,
        cursor_ms=effective_cursor,
        lag_bars=lag_bars,
        bars_needed=bars_needed,
        from_ms=from_ms,
        mode=mode,
        needs_pagination=needs_pagination,
        exceeds_pagination_ceiling=exceeds,
        max_bars=max_bars,
        single_request_limit=min(MAX_MONITOR_KLINE_LIMIT, max(DEFAULT_MONITOR_KLINE_LIMIT, bars_needed)),
    )


def replay_fetch_plan(
    *,
    setup_id: str,
    boundary_ms: int,
    now_ms: int,
    interval: str = "5m",
) -> MonitorFetchPlan:
    """Independent replay: full history from monitor boundary (audit path)."""
    step = interval_ms(interval)
    if boundary_ms % step != 0:
        boundary_ms = interval_grid_first_open_at_or_after(boundary_ms, interval)
    context_ms = STRUCTURAL_CONTEXT_BARS * step
    span_bars = max(0, (now_ms - boundary_ms) // step + CLOSE_BUFFER_BARS)
    bars_needed = span_bars + STRUCTURAL_CONTEXT_BARS
    from_ms = max(0, boundary_ms - context_ms)
    exceeds = bars_needed > MONITOR_PAGINATED_MAX_BARS
    capped = min(bars_needed, MONITOR_PAGINATED_MAX_BARS)
    mode = "paginated" if bars_needed > MAX_MONITOR_KLINE_LIMIT else "extended_window"
    return MonitorFetchPlan(
        setup_id=setup_id,
        boundary_ms=boundary_ms,
        cursor_ms=boundary_ms - step,
        lag_bars=span_bars,
        bars_needed=bars_needed,
        from_ms=from_ms,
        mode=mode if bars_needed > DEFAULT_MONITOR_KLINE_LIMIT else "recent_window",
        needs_pagination=bars_needed > MAX_MONITOR_KLINE_LIMIT,
        exceeds_pagination_ceiling=exceeds,
        max_bars=capped,
        single_request_limit=min(MAX_MONITOR_KLINE_LIMIT, max(DEFAULT_MONITOR_KLINE_LIMIT, capped)),
    )


def monitor_kline_limit_for_setups(
    *,
    monitor_cursor_ms: int,
    monitor_from_ms: int,
    now_ms: int,
    interval: str = "5m",
) -> tuple[int, dict]:
    """Legacy aggregate limit (single-window fetch cap 500)."""
    plan = live_monitor_fetch_plan(
        setup_id="",
        boundary_ms=monitor_from_ms,
        cursor_ms=monitor_cursor_ms,
        now_ms=now_ms,
        interval=interval,
    )
    meta: dict = {
        "default_limit": DEFAULT_MONITOR_KLINE_LIMIT,
        "cursor_ms": monitor_cursor_ms,
        "boundary_ms": monitor_from_ms,
        "now_ms": now_ms,
        "lag_bars_estimate": plan.lag_bars,
        "bars_needed": plan.bars_needed,
        "chosen_limit": plan.single_request_limit,
        "backfill": plan.bars_needed > DEFAULT_MONITOR_KLINE_LIMIT,
        "mode": plan.mode,
    }
    if monitor_cursor_ms < 0:
        return DEFAULT_MONITOR_KLINE_LIMIT, {**meta, "reason": "cursor_uninitialized"}
    return plan.single_request_limit, meta


def aggregate_monitor_kline_limit(setups: list, *, now_ms: int, interval: str = "5m") -> tuple[int, list[dict]]:
    """Worst-case single-request limit across active setups (≤500)."""
    if not setups:
        return DEFAULT_MONITOR_KLINE_LIMIT, []
    per_setup: list[dict] = []
    chosen = DEFAULT_MONITOR_KLINE_LIMIT
    for setup in setups:
        confirm_boundary = setup.monitor_from_ms or 0
        boundary = (
            interval_grid_first_open_at_or_after(confirm_boundary, interval)
            if confirm_boundary % interval_ms(interval) != 0
            else confirm_boundary
        )
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
