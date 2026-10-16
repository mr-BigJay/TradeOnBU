from __future__ import annotations

from types import SimpleNamespace

from app.services.monitor_kline_backfill import (
    DEFAULT_MONITOR_KLINE_LIMIT,
    MAX_MONITOR_KLINE_LIMIT,
    aggregate_monitor_kline_limit,
    detect_skipped_closed_bars,
    monitor_kline_limit_for_setups,
)
from app.domain.candles import Candle


def test_monitor_kline_limit_default_when_cursor_uninitialized():
    lim, meta = monitor_kline_limit_for_setups(
        monitor_cursor_ms=-1,
        monitor_from_ms=0,
        now_ms=1_000_000,
    )
    assert lim == DEFAULT_MONITOR_KLINE_LIMIT
    assert meta["reason"] == "cursor_uninitialized"


def test_monitor_kline_limit_backfill_when_lag_exceeds_default_window():
    step = 300_000
    now_ms = 1_000_000_000
    cursor = now_ms - 150 * step
    lim, meta = monitor_kline_limit_for_setups(
        monitor_cursor_ms=cursor,
        monitor_from_ms=cursor - 10 * step,
        now_ms=now_ms,
    )
    assert lim > DEFAULT_MONITOR_KLINE_LIMIT
    assert meta["backfill"] is True
    assert lim <= MAX_MONITOR_KLINE_LIMIT


def test_aggregate_monitor_kline_limit_takes_max_across_setups():
    now_ms = 2_000_000_000
    step = 300_000
    s1 = SimpleNamespace(setup_id="a", monitor_cursor_ms=now_ms - 50 * step, monitor_from_ms=now_ms - 60 * step)
    s2 = SimpleNamespace(setup_id="b", monitor_cursor_ms=now_ms - 200 * step, monitor_from_ms=now_ms - 210 * step)
    lim, per = aggregate_monitor_kline_limit([s1, s2], now_ms=now_ms)
    assert lim > DEFAULT_MONITOR_KLINE_LIMIT
    assert len(per) == 2


def test_detect_skipped_closed_bars_flags_missing_opens():
    step = 300_000
    boundary = 1_000_000
    cursor = boundary
    now_ms = boundary + 5 * step + step  # allow last bar to be "closed"
    bars = [
        Candle(time_ms=boundary, open=1, high=1, low=1, close=1, volume=1),
        Candle(time_ms=boundary + 2 * step, open=1, high=1, low=1, close=1, volume=1),
        Candle(time_ms=boundary + 4 * step, open=1, high=1, low=1, close=1, volume=1),
    ]
    skipped = detect_skipped_closed_bars(
        closed_bars=bars,
        cursor_ms=cursor,
        boundary_ms=boundary,
        now_ms=now_ms,
    )
    assert boundary + step in skipped
    assert boundary + 3 * step in skipped
