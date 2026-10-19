"""Fetch 5m monitor history with pagination when cursor lag exceeds a single Bitunix window."""

from __future__ import annotations

from typing import Any

from app.domain.candle_utils import interval_ms, split_closed_and_forming
from app.domain.candles import Candle
from app.services.market_data import KlineBundle, MarketDataService
from app.services.monitor_kline_backfill import (
    DEFAULT_MONITOR_KLINE_LIMIT,
    MONITOR_PAGINATED_MAX_BARS,
    MonitorFetchPlan,
    live_monitor_fetch_plan,
    replay_fetch_plan,
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


def _plan_to_dict(plan: MonitorFetchPlan) -> dict[str, Any]:
    return {
        "setup_id": plan.setup_id,
        "mode": plan.mode,
        "lag_bars": plan.lag_bars,
        "bars_needed": plan.bars_needed,
        "from_ms": plan.from_ms,
        "max_bars": plan.max_bars,
        "needs_pagination": plan.needs_pagination,
        "exceeds_pagination_ceiling": plan.exceeds_pagination_ceiling,
        "history_sufficient": not plan.exceeds_pagination_ceiling,
    }


def _merge_closed(*bundles: KlineBundle) -> dict[int, Candle]:
    merged: dict[int, Candle] = {}
    for b in bundles:
        for c in b.closed:
            merged[c.time_ms] = c
    return merged


def _build_merged_bundle(
    merged: dict[int, Candle],
    *,
    now_ms: int,
    recent_complete: bool,
    deep_complete: bool,
) -> KlineBundle:
    candles = sorted(merged.values(), key=lambda c: c.time_ms)
    from app.domain.candle_utils import detect_gaps

    gaps_remaining = detect_gaps(candles, MONITOR_INTERVAL) if len(candles) >= 2 else []
    closed, forming = split_closed_and_forming(candles, MONITOR_INTERVAL, now_ms)
    return KlineBundle(
        closed=closed,
        forming=forming,
        gaps_detected=[],
        gaps_remaining=gaps_remaining,
        data_complete=recent_complete and deep_complete and len(gaps_remaining) == 0,
        interval=MONITOR_INTERVAL,
    )


async def fetch_monitor_5m_bundle(
    market: MarketDataService,
    symbol: str,
    setups: list,
    *,
    now_ms: int,
) -> tuple[KlineBundle, dict[str, Any]]:
    """
    Live monitor fetch: cursor-anchored plans per setup, merged candles.

    One setup with insufficient history does not block others; per-setup coverage
    is recorded in meta for fail-closed gap handling in the monitor loop.
    """
    if not setups:
        bundle = await market.fetch_kline_bundle(symbol, MONITOR_INTERVAL, DEFAULT_MONITOR_KLINE_LIMIT)
        return bundle, {"fetch_mode": "recent_window", "per_setup": [], "aggregate_limit": DEFAULT_MONITOR_KLINE_LIMIT}

    plans: list[MonitorFetchPlan] = []
    from app.domain.setup_monitor_boundary import five_m_monitor_anchor_ms

    for setup in setups:
        confirm_boundary = _monitor_boundary_ms(setup)
        boundary = five_m_monitor_anchor_ms(confirm_boundary)
        cursor = _monitor_cursor_effective(setup, confirm_boundary)
        plans.append(
            live_monitor_fetch_plan(
                setup_id=setup.setup_id,
                boundary_ms=boundary,
                cursor_ms=cursor,
                now_ms=now_ms,
            )
        )

    any_pagination = any(p.needs_pagination for p in plans)
    meta: dict[str, Any] = {
        "fetch_mode": "paginated_merged" if any_pagination else "recent_window",
        "per_setup": [_plan_to_dict(p) for p in plans],
        "aggregate_limit": max((p.single_request_limit for p in plans), default=DEFAULT_MONITOR_KLINE_LIMIT),
        "paginated_max_bars_ceiling": MONITOR_PAGINATED_MAX_BARS,
    }

    recent_limit = min(BITUNIX_KLINE_BATCH, max(DEFAULT_MONITOR_KLINE_LIMIT, meta["aggregate_limit"]))
    recent = await market.fetch_kline_bundle(symbol, MONITOR_INTERVAL, recent_limit)
    bundles: list[KlineBundle] = [recent]
    deep_complete = True

    for plan in plans:
        if plan.exceeds_pagination_ceiling:
            deep_complete = False
            continue
        if not plan.needs_pagination and plan.mode == "recent_window":
            continue
        if plan.needs_pagination or plan.mode == "extended_window":
            hist = await market.fetch_historical_closed(
                symbol,
                MONITOR_INTERVAL,
                from_ms=plan.from_ms,
                max_bars=plan.max_bars,
            )
            bundles.append(hist)
            if not hist.data_complete:
                deep_complete = False

    merged = _merge_closed(*bundles)
    bundle = _build_merged_bundle(
        merged,
        now_ms=now_ms,
        recent_complete=recent.data_complete,
        deep_complete=deep_complete,
    )
    meta["recent_data_complete"] = recent.data_complete
    meta["deep_fetch_complete"] = deep_complete
    meta["merged_bar_count"] = len(merged)
    return bundle, meta


async def fetch_replay_5m_bundle(
    market: MarketDataService,
    symbol: str,
    setups: list,
    *,
    now_ms: int,
) -> tuple[KlineBundle, dict[str, Any]]:
    """Replay audit: history from monitor boundary (not cursor-anchored)."""
    if not setups:
        bundle = await market.fetch_kline_bundle(symbol, MONITOR_INTERVAL, DEFAULT_MONITOR_KLINE_LIMIT)
        return bundle, {"fetch_mode": "replay_empty", "per_setup": []}

    plans: list[MonitorFetchPlan] = []
    from app.domain.setup_monitor_boundary import five_m_monitor_anchor_ms

    for setup in setups:
        confirm_boundary = _monitor_boundary_ms(setup)
        boundary = five_m_monitor_anchor_ms(confirm_boundary)
        plans.append(
            replay_fetch_plan(
                setup_id=setup.setup_id,
                boundary_ms=boundary,
                now_ms=now_ms,
            )
        )

    meta: dict[str, Any] = {
        "fetch_mode": "replay_boundary",
        "per_setup": [_plan_to_dict(p) for p in plans],
        "from_ms": min(p.from_ms for p in plans),
        "paginated_max_bars_ceiling": MONITOR_PAGINATED_MAX_BARS,
    }

    bundles: list[KlineBundle] = []
    deep_complete = True
    for plan in plans:
        if plan.exceeds_pagination_ceiling:
            deep_complete = False
            continue
        hist = await market.fetch_historical_closed(
            symbol,
            MONITOR_INTERVAL,
            from_ms=plan.from_ms,
            max_bars=plan.max_bars,
        )
        bundles.append(hist)
        if not hist.data_complete:
            deep_complete = False

    if not bundles:
        empty = KlineBundle(
            closed=[],
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=False,
            interval=MONITOR_INTERVAL,
        )
        return empty, meta

    merged = _merge_closed(*bundles)
    bundle = _build_merged_bundle(
        merged,
        now_ms=now_ms,
        recent_complete=True,
        deep_complete=deep_complete,
    )
    meta["deep_fetch_complete"] = deep_complete
    meta["merged_bar_count"] = len(merged)
    return bundle, meta


def setup_history_covers_cursor(
    *,
    plan: MonitorFetchPlan,
    closed_bars: list[Candle],
    now_ms: int,
) -> tuple[bool, str | None]:
    """Per-setup fail-closed coverage check against merged bundle."""
    from app.services.monitor_kline_backfill import detect_skipped_closed_bars

    if plan.exceeds_pagination_ceiling:
        return False, "lag_exceeds_pagination_ceiling"
    if not closed_bars:
        return False, "no_bars_in_merged_bundle"
    if plan.mode == "recent_window" and not plan.needs_pagination:
        in_window = list(closed_bars)
    else:
        in_window = [c for c in closed_bars if c.time_ms >= plan.from_ms]
        if not in_window:
            return False, "no_bars_after_plan_from_ms"
        step = interval_ms(MONITOR_INTERVAL)
        if min(c.time_ms for c in in_window) > plan.from_ms + step:
            return False, "merged_bundle_starts_after_required_from_ms"
    skipped = detect_skipped_closed_bars(
        closed_bars=in_window,
        cursor_ms=plan.cursor_ms,
        boundary_ms=plan.boundary_ms,
        now_ms=now_ms,
    )
    if skipped:
        return False, "gap_in_required_span"
    return True, None
