"""Order closed-candle invalidations, consumed opportunities and Paper limit fills."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.domain.candle_utils import candle_close_ms, interval_ms
from app.domain.candles import Candle
from app.domain.market_freshness import is_market_data_fresh
from app.services.ict_confirmation_engine import _check_invalidation_on_bar
from app.services.position.monitor_gap_utils import gaps_remaining_block_processing

INTERVAL_5M = "5m"
INTERVAL_1M = "1m"

ReconcileAction = Literal["hold", "suspend", "cancel", "fill"]


def bar_hits_limit(bar: Candle, limit: float, direction: str | None = None) -> bool:
    """A resting buy fills at-or-below its limit; sell at-or-above.

    A gap entirely beyond the limit is marketable, not a missed touch.
    Callers must exclude candles that opened before order placement because
    their intra-minute chronology is unprovable from OHLC.
    """
    if direction == "LONG":
        return bar.low <= limit
    if direction == "SHORT":
        return bar.high >= limit
    # Backwards-compatible range-only behavior for legacy callers.
    return bar.low <= limit <= bar.high


def verify_five_m_for_pending_fills(
    *,
    data_complete: bool,
    closed_5m: list[Candle],
    now_ms: int,
    configured_stale_seconds: int,
    order_ms: int,
    monitor_cursor_ms: int | None,
) -> tuple[bool, str | None]:
    if not data_complete or not closed_5m:
        return False, "missing_or_incomplete_5m_data"
    last_closed = closed_5m[-1]
    fresh, _, _ = is_market_data_fresh(
        last_closed, INTERVAL_5M, now_ms, configured_stale_seconds
    )
    if not fresh:
        return False, "stale_5m_market_data"
    cursor = monitor_cursor_ms if monitor_cursor_ms is not None else -1
    coverage_from = max(order_ms, cursor + 1)
    oldest_open = closed_5m[0].time_ms
    step_5m = interval_ms(INTERVAL_5M)
    if oldest_open > coverage_from + step_5m:
        return False, "insufficient_5m_history_coverage"
    return True, None


def _align_1m_open_ms(t_ms: int, step: int) -> int:
    return (t_ms // step) * step


def one_m_history_rules_out_fill_before_invalidation(
    *,
    order_ms: int,
    invalidation_close_ms: int,
    closed_1m: list[Candle],
    one_m_data_complete: bool,
    gaps_remaining: list[int] | None = None,
) -> bool:
    """
    True only when every closed 1m slot from the order candle through the candle
    that closes at ``invalidation_close_ms`` is present (no interior gaps).
    """
    if not one_m_data_complete or invalidation_close_ms < order_ms:
        return False
    step = interval_ms(INTERVAL_1M)
    first_open = _align_1m_open_ms(order_ms, step)
    last_open = invalidation_close_ms - step
    if last_open < first_open:
        return False

    by_open: dict[int, Candle] = {}
    for bar in closed_1m:
        open_ms = bar.time_ms
        if open_ms < first_open or open_ms > last_open:
            continue
        if open_ms % step != 0:
            return False
        if open_ms in by_open:
            return False
        by_open[open_ms] = bar

    cursor = first_open - step if first_open >= step else 0
    if gaps_remaining_block_processing(
        gaps_remaining or [], cursor, last_open, step
    ):
        return False

    open_ms = first_open
    while open_ms <= last_open:
        bar = by_open.get(open_ms)
        if bar is None:
            return False
        close_ms = candle_close_ms(bar, INTERVAL_1M)
        if close_ms < order_ms or close_ms > invalidation_close_ms:
            return False
        open_ms += step
    return True


def unprocessed_closed_5m_bars_before_1m(
    *,
    monitor_cursor_ms: int | None,
    closed_5m: list[Candle],
    bar_1m: Candle,
    now_ms: int,
) -> list[Candle]:
    cursor = monitor_cursor_ms if monitor_cursor_ms is not None else -1
    one_m_close = candle_close_ms(bar_1m, INTERVAL_1M)
    out: list[Candle] = []
    for bar in closed_5m:
        if bar.time_ms <= cursor:
            continue
        five_m_close = candle_close_ms(bar, INTERVAL_5M)
        if now_ms < five_m_close:
            continue
        if five_m_close <= one_m_close:
            out.append(bar)
    return out


@dataclass(frozen=True)
class PendingLimitReconciliation:
    action: ReconcileAction
    reason: str | None = None
    fill_bar_1m: Candle | None = None
    invalidation_bar_5m: Candle | None = None
    terminal_bar_1m: Candle | None = None


def _collect_reconciliation_events(
    *,
    order_ms: int,
    direction: str,
    invalidation_level: float | None,
    monitor_cursor_ms: int | None,
    limit_price: float,
    closed_1m: list[Candle],
    closed_5m: list[Candle],
    now_ms: int,
) -> list[tuple[int, str, Candle]]:
    cursor = monitor_cursor_ms if monitor_cursor_ms is not None else -1
    events: list[tuple[int, str, Candle]] = []

    for bar in closed_5m:
        if bar.time_ms <= cursor:
            continue
        close_ms = candle_close_ms(bar, INTERVAL_5M)
        if now_ms < close_ms:
            continue
        if close_ms < order_ms:
            continue
        if _check_invalidation_on_bar(
            bar, direction=direction, invalidation_level=invalidation_level
        ):
            events.append((close_ms, "inv", bar))

    for bar in closed_1m:
        if bar.time_ms < order_ms:
            continue
        close_ms = candle_close_ms(bar, INTERVAL_1M)
        if now_ms < close_ms:
            continue
        if close_ms < order_ms:
            continue
        if bar_hits_limit(bar, limit_price, direction):
            events.append((close_ms, "fill", bar))

    events.sort(key=lambda item: (item[0], 0 if item[1] == "inv" else 1))
    return events


def reconcile_pending_ai_limit_order(
    *,
    order_ms: int,
    direction: str,
    invalidation_level: float | None,
    monitor_cursor_ms: int | None,
    limit_price: float,
    closed_1m: list[Candle],
    closed_5m: list[Candle],
    now_ms: int,
    five_m_data_verified: bool,
    one_m_data_complete: bool = True,
    gaps_remaining: list[int] | None = None,
    target_price: float | None = None,
    entry_zone: tuple[float, float] | None = None,
) -> PendingLimitReconciliation:
    """
    Event-time merge of closed 5m invalidations vs 1m limit touches after order creation.
    """
    if not five_m_data_verified:
        return PendingLimitReconciliation(
            action="suspend",
            reason="missing_or_incomplete_5m_data",
        )

    events = _collect_reconciliation_events(
        order_ms=order_ms,
        direction=direction,
        invalidation_level=invalidation_level,
        monitor_cursor_ms=monitor_cursor_ms,
        limit_price=limit_price,
        closed_1m=closed_1m,
        closed_5m=closed_5m,
        now_ms=now_ms,
    )
    # Zone contact alone neither fills nor cancels a limit order.
    # Keep entry_zone in the API for existing callers; only actual limit/target/
    # invalidation events affect execution.
    if target_price is not None:
        for candidate in closed_1m:
            close_ms = candle_close_ms(candidate, INTERVAL_1M)
            if close_ms <= order_ms or close_ms > now_ms:
                continue
            hits_target = candidate.high >= target_price if direction == "LONG" else candidate.low <= target_price
            if hits_target:
                events.append((close_ms, "target_ambiguous" if candidate.time_ms < order_ms else "target", candidate))
    # Target and fill inside one minute have unknown ordering: never invent a fill.
    events.sort(key=lambda item: (item[0], 0 if item[1].startswith("target") else 1 if item[1] == "inv" else 2))
    if not events:
        return PendingLimitReconciliation(action="hold")

    first_time, kind, bar = events[0]
    if kind.startswith("target"):
        if kind == "target_ambiguous" or bar_hits_limit(bar, limit_price, direction):
            return PendingLimitReconciliation(action="suspend", reason="target_fill_chronology_ambiguous", terminal_bar_1m=bar)
        if not one_m_history_rules_out_fill_before_invalidation(
            order_ms=order_ms, invalidation_close_ms=first_time, closed_1m=closed_1m,
            one_m_data_complete=one_m_data_complete, gaps_remaining=gaps_remaining,
        ):
            return PendingLimitReconciliation(action="suspend", reason="insufficient_1m_history_for_target_cancel", terminal_bar_1m=bar)
        # An order-creation candle might contain an unprovable post-order touch.
        if any(c.time_ms < order_ms < candle_close_ms(c, INTERVAL_1M) and bar_hits_limit(c, limit_price, direction) for c in closed_1m):
            return PendingLimitReconciliation(action="suspend", reason="target_fill_chronology_ambiguous", terminal_bar_1m=bar)
        return PendingLimitReconciliation(action="cancel", reason="target_reached_before_fill", terminal_bar_1m=bar)
    if kind == "inv":
        if not one_m_history_rules_out_fill_before_invalidation(
            order_ms=order_ms,
            invalidation_close_ms=first_time,
            closed_1m=closed_1m,
            one_m_data_complete=one_m_data_complete,
            gaps_remaining=gaps_remaining,
        ):
            return PendingLimitReconciliation(
                action="suspend",
                reason="insufficient_1m_history_for_invalidation_cancel",
                invalidation_bar_5m=bar,
            )
        return PendingLimitReconciliation(
            action="cancel",
            reason="structural_invalidation_before_fill",
            invalidation_bar_5m=bar,
        )
    return PendingLimitReconciliation(
        action="fill",
        reason="limit_fill_before_invalidation",
        fill_bar_1m=bar,
    )


def pending_limit_fill_blocked_by_5m_backlog(
    *,
    direction: str,
    invalidation_level: float | None,
    monitor_cursor_ms: int | None,
    closed_5m: list[Candle],
    bar_1m: Candle,
    now_ms: int,
    five_m_data_verified: bool,
) -> tuple[bool, str | None]:
    if not five_m_data_verified:
        return True, "missing_or_incomplete_5m_data"

    pending = unprocessed_closed_5m_bars_before_1m(
        monitor_cursor_ms=monitor_cursor_ms,
        closed_5m=closed_5m,
        bar_1m=bar_1m,
        now_ms=now_ms,
    )
    if not pending:
        return False, None
    for bar in pending:
        if _check_invalidation_on_bar(
            bar, direction=direction, invalidation_level=invalidation_level
        ):
            return True, "unprocessed_5m_structural_invalidation"
    return True, "unprocessed_5m_bars_before_1m_fill"
