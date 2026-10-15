"""Timestamp ordering between 5m setup invalidation and 1m simulated limit fills."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.domain.candle_utils import candle_close_ms, interval_ms
from app.domain.candles import Candle
from app.domain.market_freshness import is_market_data_fresh
from app.services.ict_confirmation_engine import _check_invalidation_on_bar

INTERVAL_5M = "5m"
INTERVAL_1M = "1m"

ReconcileAction = Literal["hold", "suspend", "cancel", "fill"]


def bar_hits_limit(bar: Candle, limit: float) -> bool:
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


def one_m_history_rules_out_fill_before_invalidation(
    *,
    order_ms: int,
    invalidation_close_ms: int,
    closed_1m: list[Candle],
    one_m_data_complete: bool,
) -> bool:
    """
    True when closed 1m history spans order creation through invalidation close
    densely enough that an unobserved limit fill before invalidation is unlikely.
    """
    if not one_m_data_complete or not closed_1m:
        return False
    step = interval_ms(INTERVAL_1M)
    in_window = [
        b
        for b in closed_1m
        if order_ms <= candle_close_ms(b, INTERVAL_1M) <= invalidation_close_ms
    ]
    if not in_window:
        return False
    closes = sorted(candle_close_ms(b, INTERVAL_1M) for b in in_window)
    if closes[0] > order_ms + step * 2:
        return False
    if closes[-1] < invalidation_close_ms - step:
        return False
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
        if bar_hits_limit(bar, limit_price):
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
    if not events:
        return PendingLimitReconciliation(action="hold")

    first_time, kind, bar = events[0]
    if kind == "inv":
        if not one_m_history_rules_out_fill_before_invalidation(
            order_ms=order_ms,
            invalidation_close_ms=first_time,
            closed_1m=closed_1m,
            one_m_data_complete=one_m_data_complete,
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
