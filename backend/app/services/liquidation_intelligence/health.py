from __future__ import annotations

from typing import Any

from app.models import LiquidationSourceStatus
from app.services.liquidation_intelligence.analytics import WINDOW_MS
from app.services.liquidation_intelligence.coverage import window_connection_coverage
from app.services.liquidation_intelligence.fetch_result import FetchEventsResult


def effective_connection_activity_ms(
    *,
    last_connection_activity_at_ms: int | None,
    last_heartbeat_at_ms: int | None,
    last_event_at_ms: int | None,
) -> int | None:
    candidates = [v for v in (last_connection_activity_at_ms, last_heartbeat_at_ms, last_event_at_ms) if v and v > 0]
    return max(candidates) if candidates else None


def connection_activity_state(
    *,
    connected: bool,
    last_connection_activity_at_ms: int | None,
    last_heartbeat_at_ms: int | None,
    last_event_at_ms: int | None,
    now_ms: int,
    stale_after_ms: int,
    subscription_confirmed: bool,
) -> str:
    """Connection liveness — any valid WS traffic or liquidation event counts as activity."""
    if not connected:
        return "disconnected"
    activity = effective_connection_activity_ms(
        last_connection_activity_at_ms=last_connection_activity_at_ms,
        last_heartbeat_at_ms=last_heartbeat_at_ms,
        last_event_at_ms=last_event_at_ms,
    )
    if activity is None or (now_ms - activity) > stale_after_ms:
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
    last_activity = int(st.last_connection_activity_at_ms or 0) if st and st.last_connection_activity_at_ms else 0
    sub_ok = bool(st.subscription_confirmed) if st else False
    activity = connection_activity_state(
        connected=connected,
        last_connection_activity_at_ms=last_activity or None,
        last_heartbeat_at_ms=last_hb or None,
        last_event_at_ms=last_event or None,
        now_ms=now_ms,
        stale_after_ms=stale_after_ms,
        subscription_confirmed=sub_ok,
    )
    event_fresh = last_event > 0 and (now_ms - last_event) <= stale_after_ms
    eff = effective_connection_activity_ms(
        last_connection_activity_at_ms=last_activity or None,
        last_heartbeat_at_ms=last_hb or None,
        last_event_at_ms=last_event or None,
    )
    return {
        "connection_state": activity,
        "connected": connected,
        "subscription_confirmed": sub_ok,
        "last_event_at_ms": last_event or None,
        "last_heartbeat_at_ms": last_hb or None,
        "last_connection_activity_at_ms": eff,
        "connected_since_ms": int(st.connected_since_ms) if st and st.connected_since_ms else None,
        "event_freshness": "ok" if event_fresh else ("no_events_yet" if last_event <= 0 else "stale_events"),
        "events_in_4h_lookback": len(events_in_lookback),
        "reconnect_count": int(st.reconnect_count or 0) if st else 0,
        "last_error": (st.last_error[:200] if st and st.last_error else None),
    }


def window_data_coverage(
    st: LiquidationSourceStatus | None,
    events: list[Any],
    now_ms: int,
    window_name: str,
) -> dict[str, Any]:
    ms = WINDOW_MS[window_name]
    window_start = now_ms - ms
    conn = window_connection_coverage(
        connected=bool(st.connected) if st else False,
        connected_since_ms=int(st.connected_since_ms) if st and st.connected_since_ms else None,
        coverage_gaps=(st.coverage_gaps if st else None) or [],
        now_ms=now_ms,
        window_start_ms=window_start,
        subscription_confirmed=bool(st.subscription_confirmed) if st else False,
    )
    in_window = [e for e in events if window_start <= int(e.event_timestamp_ms) < now_ms]
    event_count = len(in_window)
    conn_cov = conn["coverage"]
    if conn_cov == "unknown":
        return {
            **conn,
            "event_count": event_count,
            "metrics": None,
            "note": "Insufficient connection history for this window",
        }
    if event_count == 0 and conn_cov == "full":
        return {
            **conn,
            "coverage": "observed_none",
            "partial": False,
            "event_count": 0,
            "metrics": None,
            "note": "Healthy connection; no liquidations observed in window",
        }
    if event_count == 0:
        return {
            **conn,
            "coverage": "empty",
            "partial": conn.get("partial", True),
            "event_count": 0,
            "metrics": None,
            "note": "No events while connection coverage incomplete or unavailable",
        }
    return {
        **conn,
        "event_count": event_count,
    }


def _apply_query_truncation(
    cov: dict[str, Any],
    *,
    window_name: str,
    now_ms: int,
    fetch: FetchEventsResult | None,
) -> dict[str, Any]:
    if fetch is None or not fetch.truncated:
        return cov
    window_start = now_ms - WINDOW_MS[window_name]
    earliest = fetch.earliest_returned_ms
    window_incomplete = earliest is None or window_start < earliest or window_name == "4h"
    if not window_incomplete:
        return {
            **cov,
            "query_truncated": True,
            "query_truncation_note": "Lookback query capped; this window may still be complete",
        }
    note = fetch.compact_warning() or "Query truncated"
    return {
        **cov,
        "coverage": "truncated",
        "partial": True,
        "metrics": None,
        "query_truncated": True,
        "query_truncation_note": note,
        "earliest_event_in_sample_ms": earliest,
        "lookback_start_ms": fetch.since_ms,
    }


def build_windows_with_coverage(
    events: list[Any],
    now_ms: int,
    compute_metrics_fn,
    *,
    source_status: LiquidationSourceStatus | None,
    fetch_result: FetchEventsResult | None = None,
) -> dict[str, Any]:
    raw = compute_metrics_fn(events, now_ms)
    out: dict[str, Any] = {}
    for name in ("1m", "5m", "15m", "1h", "4h"):
        metrics = raw.get(name)
        cov = window_data_coverage(source_status, events, now_ms, name)
        cov = _apply_query_truncation(cov, window_name=name, now_ms=now_ms, fetch=fetch_result)
        if cov.get("metrics") is None and cov.get("coverage") in (
            "unknown",
            "empty",
            "observed_none",
            "truncated",
        ):
            out[name] = {**cov, "metrics": None}
            continue
        if cov.get("coverage") in ("partial", "unknown", "truncated"):
            out[name] = {
                **cov,
                "metrics": None if cov.get("coverage") == "truncated" else {**(metrics or {}), "_data_coverage": cov.get("coverage")},
            }
        else:
            out[name] = {**cov, "metrics": metrics}
    return out
