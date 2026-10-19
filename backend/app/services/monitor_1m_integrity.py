"""1m bundle integrity gates for BALANCED confirmation (fail closed)."""

from __future__ import annotations

from app.domain.candles import Candle
from app.domain.candle_utils import candle_close_ms, interval_ms


def assess_1m_monitor_integrity(
    *,
    closed_1m: list[Candle],
    data_complete: bool,
    gaps_remaining: list[int],
    boundary_ms: int,
    cursor_ms: int,
    now_ms: int,
    max_age_after_close_ms: int = 330_000,
) -> tuple[bool, str, dict]:
    """
    Returns (ok, reason, detail). When not ok, BALANCED must not stamp tokens or advance 1m cursor.
    """
    detail: dict = {
        "data_complete": data_complete,
        "gaps_count": len(gaps_remaining or []),
        "closed_count": len(closed_1m or []),
    }
    if not closed_1m:
        return False, "missing_1m_closed_bars", detail
    if not data_complete:
        return False, "1m_bundle_incomplete", detail
    if gaps_remaining:
        detail["gaps_remaining_sample"] = list(gaps_remaining[:10])
        return False, "1m_gaps_remaining", detail

    step = interval_ms("1m")

    def _freshness_fail(latest: Candle) -> tuple[bool, str] | None:
        close_ms = candle_close_ms(latest, "1m")
        if now_ms < close_ms:
            return False, "1m_bar_not_closed_at_eval"
        age = now_ms - close_ms
        detail["latest_1m_open_ms"] = latest.time_ms
        detail["age_since_1m_close_ms"] = age
        if age > max_age_after_close_ms:
            return False, "stale_1m_data"
        return None

    in_scope = [
        c
        for c in closed_1m
        if c.time_ms >= boundary_ms
        and c.time_ms > cursor_ms
        and now_ms >= candle_close_ms(c, "1m")
    ]
    if not in_scope:
        at_or_after = [c for c in closed_1m if c.time_ms >= boundary_ms]
        if at_or_after:
            latest = max(at_or_after, key=lambda c: c.time_ms)
            fail = _freshness_fail(latest)
            if fail:
                return False, fail[1], detail
        return True, "ok_no_new_bars", detail

    sorted_scope = sorted(in_scope, key=lambda c: c.time_ms)
    first = sorted_scope[0]
    if cursor_ms >= boundary_ms and first.time_ms > cursor_ms + step:
        detail["gap_from_cursor_ms"] = cursor_ms
        detail["first_new_1m_open_ms"] = first.time_ms
        return False, "1m_gap_from_cursor", detail

    latest = max(in_scope, key=lambda c: c.time_ms)
    fail = _freshness_fail(latest)
    if fail:
        return False, fail[1], detail

    for prev, nxt in zip(sorted_scope, sorted_scope[1:]):
        if nxt.time_ms - prev.time_ms > step:
            detail["discontinuity_after_ms"] = prev.time_ms
            return False, "1m_discontinuity_in_new_range", detail

    return True, "ok", detail
