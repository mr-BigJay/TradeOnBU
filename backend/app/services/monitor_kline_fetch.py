"""Fetch 5m monitor history with pagination when cursor lag exceeds a single Bitunix window."""

from __future__ import annotations

from typing import Any

from app.services.market_data import KlineBundle, MarketDataService
from app.services.monitor_kline_backfill import (
    DEFAULT_MONITOR_KLINE_LIMIT,
    MAX_MONITOR_KLINE_LIMIT,
    aggregate_monitor_kline_limit,
)
from app.domain.setup_monitor_boundary import first_eligible_bar_open_ms

MONITOR_INTERVAL = "5m"
BITUNIX_KLINE_BATCH = 200


def _monitor_boundary_ms(setup) -> int:
    if setup.monitor_from_ms is not None and setup.monitor_from_ms > 0:
        return setup.monitor_from_ms
    return first_eligible_bar_open_ms(setup.created_at, MONITOR_INTERVAL)


def _monitor_cursor_effective(setup, boundary: int) -> int:
    if setup.monitor_cursor_ms is not None and setup.monitor_cursor_ms >= 0:
        return setup.monitor_cursor_ms
    return boundary - 1


def _history_from_ms_for_setups(setups: list, *, interval: str = "5m") -> int:
    if not setups:
        return 0
    from app.domain.candle_utils import interval_ms

    step = interval_ms(interval)
    starts: list[int] = []
    for setup in setups:
        boundary = _monitor_boundary_ms(setup)
        cursor = _monitor_cursor_effective(setup, boundary)
        # Include one bar of context before boundary for swing windows.
        starts.append(max(0, min(boundary, cursor + step) - 48 * step))
    return min(starts)


async def fetch_monitor_5m_bundle(
    market: MarketDataService,
    symbol: str,
    setups: list,
    *,
    now_ms: int,
) -> tuple[KlineBundle, dict[str, Any]]:
    """
    Return 5m candles covering monitor boundaries/cursors.

    Uses paginated historical fetch when lag/backfill exceeds a single 200-candle API window.
    """
    limit, per_setup_meta = aggregate_monitor_kline_limit(setups, now_ms=now_ms, interval=MONITOR_INTERVAL)
    meta: dict[str, Any] = {
        "aggregate_limit": limit,
        "per_setup": per_setup_meta,
        "fetch_mode": "recent_window",
    }
    needs_deep = limit > DEFAULT_MONITOR_KLINE_LIMIT or any(
        m.get("backfill") for m in per_setup_meta
    )
    if not setups or not needs_deep:
        bundle = await market.fetch_kline_bundle(symbol, MONITOR_INTERVAL, min(limit, BITUNIX_KLINE_BATCH))
        return bundle, meta

    from_ms = _history_from_ms_for_setups(setups, interval=MONITOR_INTERVAL)
    # Paginate beyond 500 when lag requires it (Bitunix returns max 200 per call).
    max_bars = min(max(limit, MAX_MONITOR_KLINE_LIMIT), 2400)
    meta["fetch_mode"] = "historical_paginated"
    meta["from_ms"] = from_ms
    meta["max_bars"] = max_bars
    bundle = await market.fetch_historical_closed(
        symbol,
        MONITOR_INTERVAL,
        from_ms=from_ms,
        max_bars=max_bars,
    )
    return bundle, meta
