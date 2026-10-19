from __future__ import annotations

from typing import Any


def compact_liquidation_for_llm(full: dict[str, Any]) -> dict[str, Any]:
    """Keep GPT input small — stats only, no raw events."""
    if full.get("status") in ("disabled", "unavailable"):
        return {
            "status": full.get("status"),
            "symbol": full.get("symbol"),
            "note": full.get("note") or full.get("coverage_notes", {}).get("aggregate"),
        }
    windows = full.get("windows") or {}
    slim_windows = {k: windows.get(k) for k in ("5m", "15m", "1h", "4h") if k in windows}
    return {
        "status": full.get("status"),
        "symbol": full.get("symbol"),
        "sources": full.get("sources"),
        "source_quality": full.get("source_quality"),
        "coverage_notes": full.get("coverage_notes"),
        "windows": slim_windows,
        "price_clusters": (full.get("price_clusters") or [])[:5],
        "burst_metrics": full.get("burst_metrics"),
        "last_event_time_ms": full.get("last_event_time_ms"),
        "interpretation": full.get("interpretation"),
        "bybit_status": (full.get("bybit") or {}).get("status"),
        "binance_status": (full.get("binance") or {}).get("status"),
    }
