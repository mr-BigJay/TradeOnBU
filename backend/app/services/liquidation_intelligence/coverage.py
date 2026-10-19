from __future__ import annotations

from typing import Any

MAX_GAPS_STORED = 64
GAP_LOOKBACK_MS = 4 * 60 * 60 * 1000


def _clamp_interval(start: int, end: int, win_start: int, win_end: int) -> tuple[int, int] | None:
    lo = max(start, win_start)
    hi = min(end, win_end)
    if hi <= lo:
        return None
    return lo, hi


def connected_intervals_from_status(
    *,
    connected: bool,
    connected_since_ms: int | None,
    coverage_gaps: list[dict[str, Any]] | None,
    now_ms: int,
) -> list[tuple[int, int]]:
    """Closed gaps from JSON plus optional open interval when currently connected."""
    intervals: list[tuple[int, int]] = []
    for gap in coverage_gaps or []:
        try:
            start = int(gap.get("start_ms") or 0)
            end = int(gap.get("end_ms") or 0)
        except (TypeError, ValueError):
            continue
        if end > start > 0:
            intervals.append((start, end))
    if connected and connected_since_ms and connected_since_ms > 0:
        intervals.append((int(connected_since_ms), now_ms))
    return sorted(intervals, key=lambda x: x[0])


def connected_ms_in_window(intervals: list[tuple[int, int]], window_start_ms: int, window_end_ms: int) -> int:
    total = 0
    for start, end in intervals:
        clipped = _clamp_interval(start, end, window_start_ms, window_end_ms)
        if clipped:
            total += clipped[1] - clipped[0]
    return total


def window_connection_coverage(
    *,
    connected: bool,
    connected_since_ms: int | None,
    coverage_gaps: list[dict[str, Any]] | None,
    now_ms: int,
    window_start_ms: int,
    subscription_confirmed: bool,
) -> dict[str, Any]:
    window_end_ms = now_ms
    window_ms = max(1, window_end_ms - window_start_ms)
    intervals = connected_intervals_from_status(
        connected=connected,
        connected_since_ms=connected_since_ms,
        coverage_gaps=coverage_gaps,
        now_ms=now_ms,
    )
    if not intervals and not connected and not coverage_gaps:
        return {
            "coverage": "unknown",
            "partial": True,
            "connected_ms": 0,
            "window_ms": window_ms,
            "connected_fraction": None,
            "reason": "no_connection_history",
        }
    if connected and connected_since_ms is None and not coverage_gaps:
        return {
            "coverage": "unknown",
            "partial": True,
            "connected_ms": 0,
            "window_ms": window_ms,
            "connected_fraction": None,
            "reason": "connected_since_not_recorded",
        }
    connected_ms = connected_ms_in_window(intervals, window_start_ms, window_end_ms)
    fraction = connected_ms / window_ms if window_ms > 0 else 0.0
    if fraction >= 0.98 and subscription_confirmed:
        cov = "full"
        partial = False
    elif fraction <= 0:
        cov = "unknown" if not subscription_confirmed else "partial"
        partial = True
    else:
        cov = "partial"
        partial = True
    return {
        "coverage": cov,
        "partial": partial,
        "connected_ms": connected_ms,
        "window_ms": window_ms,
        "connected_fraction": round(fraction, 4),
        "interval_count": len(intervals),
    }


def append_coverage_gap(
    gaps: list[dict[str, Any]] | None,
    *,
    start_ms: int,
    end_ms: int,
    now_ms: int,
) -> list[dict[str, Any]]:
    out = list(gaps or [])
    out.insert(0, {"start_ms": int(start_ms), "end_ms": int(end_ms)})
    cutoff = now_ms - GAP_LOOKBACK_MS
    trimmed = [g for g in out if int(g.get("end_ms") or 0) >= cutoff]
    return trimmed[:MAX_GAPS_STORED]


def events_in_aligned_bucket(events: list[Any], bucket_start_ms: int, bucket_end_ms: int) -> list[Any]:
    return [e for e in events if bucket_start_ms <= int(e.event_timestamp_ms) < bucket_end_ms]
