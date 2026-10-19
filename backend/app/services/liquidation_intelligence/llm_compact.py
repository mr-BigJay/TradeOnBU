from __future__ import annotations

from typing import Any

from app.services.liquidation_intelligence.gpt_packaging import build_combined_labeled_windows, slim_source_block_for_llm


def compact_liquidation_for_llm(full: dict[str, Any]) -> dict[str, Any]:
    """Compact liquidation stats for GPT — validated per-source metrics, labeled combined sums."""
    if full.get("status") in ("disabled", "unavailable"):
        return {
            "status": full.get("status"),
            "symbol": full.get("symbol"),
            "note": full.get("note") or full.get("coverage_notes", {}).get("aggregate"),
        }
    bybit = slim_source_block_for_llm(full.get("bybit"))
    binance = slim_source_block_for_llm(full.get("binance"))
    combined_labeled = full.get("combined_labeled") or {}
    combined_windows = combined_labeled.get("windows") or build_combined_labeled_windows(
        bybit_block=full.get("bybit") or {},
        binance_block=full.get("binance") or {},
    )
    clusters = full.get("price_clusters")
    if isinstance(clusters, dict):
        slim_clusters = {
            "bybit": (clusters.get("bybit") or [])[:3],
            "binance": (clusters.get("binance") or [])[:3],
        }
    else:
        slim_clusters = (clusters or [])[:5]
    return {
        "status": full.get("status"),
        "symbol": full.get("symbol"),
        "sources": full.get("sources"),
        "source_quality": full.get("source_quality"),
        "coverage_notes": full.get("coverage_notes"),
        "freshness_and_coverage": full.get("freshness_and_coverage"),
        "bybit": bybit,
        "binance": binance,
        "combined_labeled": {
            "status": combined_labeled.get("status"),
            "windows": {k: combined_windows.get(k) for k in ("5m", "15m", "1h", "4h") if k in combined_windows},
            "total_observed_notional_usd_1h": combined_labeled.get("total_observed_notional_usd_1h"),
            "note": combined_labeled.get("note"),
        },
        "price_clusters": slim_clusters,
        "last_event_time_ms": full.get("last_event_time_ms"),
        "interpretation": full.get("interpretation"),
    }
