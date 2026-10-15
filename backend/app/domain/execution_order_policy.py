"""Timestamp ordering between 5m setup invalidation and 1m simulated limit fills."""

from __future__ import annotations

from app.domain.candle_utils import candle_close_ms
from app.domain.candles import Candle
from app.services.ict_confirmation_engine import _check_invalidation_on_bar

INTERVAL_5M = "5m"
INTERVAL_1M = "1m"


def unprocessed_closed_5m_bars_before_1m(
    *,
    monitor_cursor_ms: int | None,
    closed_5m: list[Candle],
    bar_1m: Candle,
    now_ms: int,
) -> list[Candle]:
    """
    Closed 5m bars that opportunity monitor should have applied before simulating
    a fill on ``bar_1m`` (conservative when worker ordering or data lag).
    """
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


def pending_limit_fill_blocked_by_5m_backlog(
    *,
    direction: str,
    invalidation_level: float | None,
    monitor_cursor_ms: int | None,
    closed_5m: list[Candle],
    bar_1m: Candle,
    now_ms: int,
) -> tuple[bool, str | None]:
    """
    Returns (blocked, reason). Blocks when unprocessed closed 5m bars exist at or
    before this 1m bar's close — especially when any would structurally invalidate.
    """
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
