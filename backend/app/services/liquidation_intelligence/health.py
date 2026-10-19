from __future__ import annotations

from typing import Any

from app.models import LiquidationSourceStatus
from app.services.liquidation_intelligence.analytics import WINDOW_MS


def connection_activity_state(
    *,
    connected: bool,
    last_heartbeat_at_ms: int | None,
    last_event_at_ms: int | None,
    now_ms: int,
    stale_after_ms: int,
    subscription_confirmed: bool,
) -> str:
    """Distinct from event freshness: idle connected socket with no liquidations is healthy."""
    if not connected:
        return "disconnected"
    hb = last_heartbeat_at_ms or 0
    if hb <= 0 or (now_ms - hb) > stale_after_ms:
        return "stale"
    if not subscription_confirmed and last_event_at_ms is None:
        return "connecting"
    if last_event_at_ms is None or (now_ms - last_event_at_ms) > stale_after_ms:
        return "connected_idle"
    return "connected_active"


def source_health_block(
    st: LiquidationSourceStatus | None,
    *,
    now_ms: int,
    stale_after_ms: int,
    events_in_lookback: list[Any],
) -> dict[str, Any]:
    connected = bool(st.connected) if st else False
    last_event = int(st.last_event_at_ms or st.last_message_at_ms or 0) if st else 0
    last_hb = int(st.last_heartbeat_at_ms or 0) if st else 0
    sub_ok = bool(st.subscription_confirmed) if st else False
    activity = connection_activity_state(
        connected=connected,
        last_heartbeat_at_ms=last_hb or None,
        last_event_at_ms=last_event or None,
        now_ms=now_ms,
        stale_after_ms=stale_after_ms,
        subscription_confirmed=sub_ok,
    )
    event_fresh = last_event > 0 and (now_ms - last_event) <= stale_after_ms
    return {
        "connection_state": activity,
        "connected": connected,
        "subscription_confirmed": sub_ok,
        "last_event_at_ms": last_event or None,
        "last_heartbeat_at_ms": last_hb or None,
        "event_freshness": "ok" if event_fresh else ("no_events_yet" if last_event <= 0 else "stale_events"),
        "events_in_4h_lookback": len(events_in_lookback),
        "reconnect_count": int(st.reconnect_count or 0) if st else 0,
        "last_error": (st.last_error[:200] if st and st.last_error else None),
    }


def window_coverage_for_events(events: list[Any], now_ms: int, window_name: str) -> dict[str, Any]:
    ms = WINDOW_MS[window_name]
    window_start = now_ms - ms
    in_window = [e for e in events if int(e.event_timestamp_ms) >= window_start]
    if not in_window:
        return {
            "coverage": "empty",
            "partial": False,
            "event_count": 0,
            "metrics": None,
        }
    earliest = min(int(e.event_timestamp_ms) for e in in_window)
    partial = earliest > window_start + 60_000
    return {
        "coverage": "partial" if partial else "full",
        "partial": partial,
        "window_start_ms": window_start,
        "earliest_event_ms": earliest,
        "event_count": len(in_window),
    }


def build_windows_with_coverage(events: list[Any], now_ms: int, compute_metrics_fn) -> dict[str, Any]:
    """Attach coverage; omit zero metrics when window has no valid observation period."""
    raw = compute_metrics_fn(events, now_ms)
    out: dict[str, Any] = {}
    for name, metrics in raw.items():
        cov = window_coverage_for_events(events, now_ms, name)
        if cov["coverage"] == "empty":
            out[name] = {
                "coverage": "empty",
                "partial": False,
                "note": "No liquidation events in window — not equivalent to zero market liquidations",
                "metrics": None,
            }
        else:
            out[name] = {
                **cov,
                "metrics": metrics if not cov["partial"] else {**metrics, "_partial_window": True},
            }
    return out
