"""Timestamp ordering between 5m setup invalidation and 1m simulated limit fills."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.domain.candle_utils import candle_close_ms
from app.domain.candles import Candle
from app.services.ict_confirmation_engine import _check_invalidation_on_bar

INTERVAL_5M = "5m"
INTERVAL_1M = "1m"

ReconcileAction = Literal["hold", "suspend", "cancel", "fill"]


def bar_hits_limit(bar: Candle, limit: float) -> bool:
    return bar.low <= limit <= bar.high


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

    # Same close time: invalidation before fill (conservative).
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
