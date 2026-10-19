from __future__ import annotations

from typing import Any

LLM_WINDOW_KEYS = ("5m", "15m", "1h", "4h")


def _metrics_usable(coverage: str | None) -> bool:
    return coverage not in (None, "unknown", "empty", "observed_none", "truncated")


def _sum_metric_dicts(parts: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not parts:
        return None
    total_long = 0.0
    total_short = 0.0
    total_events = 0
    largest = 0.0
    for m in parts:
        total_long += float(m.get("long_liquidation_notional_usd") or 0)
        total_short += float(m.get("short_liquidation_notional_usd") or 0)
        total_events += int(m.get("event_count") or 0)
        largest = max(largest, float(m.get("largest_observed_usd") or 0))
    total = total_long + total_short
    ratio = (total_long / total_short) if total_short > 0 else None
    return {
        "long_liquidation_notional_usd": round(total_long, 2),
        "short_liquidation_notional_usd": round(total_short, 2),
        "total_observed_notional_usd": round(total, 2),
        "long_short_ratio": round(ratio, 4) if ratio is not None else None,
        "event_count": total_events,
        "largest_observed_usd": round(largest, 2),
    }


def slim_windows_for_llm(windows: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in LLM_WINDOW_KEYS:
        w = (windows or {}).get(key) or {}
        cov = w.get("coverage")
        metrics = w.get("metrics") if _metrics_usable(str(cov) if cov else None) else None
        out[key] = {
            "coverage": cov,
            "partial": w.get("partial"),
            "connected_fraction": w.get("connected_fraction"),
            "event_count": w.get("event_count"),
            "query_truncated": w.get("query_truncated"),
            "metrics": metrics,
        }
    return out


def slim_source_block_for_llm(block: dict[str, Any] | None) -> dict[str, Any]:
    block = block or {}
    q = block.get("event_query_quality") or {}
    return {
        "status": block.get("status"),
        "source_quality": block.get("source_quality"),
        "connection_state": block.get("connection_state"),
        "event_freshness": block.get("event_freshness"),
        "event_query_quality": {
            "truncated": q.get("truncated"),
            "warning": q.get("warning"),
            "rows_returned": q.get("rows_returned"),
            "row_limit": q.get("row_limit"),
        },
        "windows": slim_windows_for_llm(block.get("windows")),
        "burst_metrics": block.get("burst_metrics") or {},
    }


def build_combined_labeled_windows(
    *,
    bybit_block: dict[str, Any],
    binance_block: dict[str, Any],
) -> dict[str, Any]:
    combined: dict[str, Any] = {}
    for key in LLM_WINDOW_KEYS:
        b = (bybit_block.get("windows") or {}).get(key) or {}
        n = (binance_block.get("windows") or {}).get(key) or {}
        contributors: list[str] = []
        metric_parts: list[dict[str, Any]] = []
        coverages: list[str] = []
        for name, w in (("bybit", b), ("binance", n)):
            cov = str(w.get("coverage") or "unknown")
            coverages.append(cov)
            if _metrics_usable(cov) and w.get("metrics"):
                contributors.append(name)
                metric_parts.append(w["metrics"])
        if not contributors:
            combined[key] = {
                "coverage": "unknown",
                "partial": True,
                "contributing_sources": [],
                "metrics": None,
                "note": "No source with validated metrics for this window",
            }
            continue
        merged = _sum_metric_dicts(metric_parts)
        combined[key] = {
            "coverage": "partial_labeled_sum",
            "partial": True,
            "contributing_sources": contributors,
            "metrics": merged,
            "note": "Sum of labeled exchange totals — not complete market-wide liquidation volume",
        }
        if len(contributors) == 1:
            combined[key]["coverage"] = "single_source_labeled"
    return combined


def build_combined_labeled_summary(
    *,
    bybit_block: dict[str, Any],
    binance_block: dict[str, Any],
    combined_events: bool,
) -> dict[str, Any]:
    windows = build_combined_labeled_windows(bybit_block=bybit_block, binance_block=binance_block)
    w1h = windows.get("1h") or {}
    return {
        "status": "partial_labeled" if combined_events else "unavailable",
        "windows": windows,
        "total_observed_notional_usd_1h": (w1h.get("metrics") or {}).get("total_observed_notional_usd"),
        "note": "Dollar totals only — not complete market-wide liquidation volume",
    }
