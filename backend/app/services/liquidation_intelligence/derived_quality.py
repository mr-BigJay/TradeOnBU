from __future__ import annotations

from typing import Any

from app.services.liquidation_intelligence.fetch_result import FetchEventsResult


def window_query_truncated(windows: dict[str, Any], window_name: str) -> bool:
    w = (windows or {}).get(window_name) or {}
    return w.get("coverage") == "truncated"


def apply_derived_analytics_quality(
    *,
    windows: dict[str, Any],
    burst_metrics: dict[str, Any] | None,
    price_clusters: list[Any] | None,
    fetch: FetchEventsResult | None,
) -> tuple[dict[str, Any], list[Any], dict[str, Any]]:
    """Suppress burst/clusters when their input windows are query-truncated."""
    meta: dict[str, Any] = {"query_truncated": bool(fetch and fetch.truncated)}
    burst = dict(burst_metrics or {})
    clusters = list(price_clusters or [])

    burst_bad = window_query_truncated(windows, "5m") or window_query_truncated(windows, "15m")
    if burst_bad:
        burst = {
            "burst": False,
            "intensity_vs_baseline": None,
            "unreliable": True,
            "reason": "query_truncation_affects_5m_or_15m",
        }
        meta["burst_metrics"] = "suppressed"

    cluster_bad = window_query_truncated(windows, "1h")
    if cluster_bad:
        clusters = []
        meta["price_clusters"] = "suppressed"

    return burst, clusters, meta
