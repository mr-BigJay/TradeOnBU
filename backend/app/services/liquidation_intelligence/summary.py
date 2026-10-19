from __future__ import annotations

import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.services.liquidation_intelligence.analytics import (
    WINDOW_MS,
    aggregate_observed_notional_total,
    build_per_source_analytics,
    compute_window_metrics,
)
from app.services.liquidation_intelligence.collector import get_liquidation_collector
from app.services.liquidation_intelligence.health import build_windows_with_coverage, source_health_block
from app.services.liquidation_intelligence.repository import all_source_status, fetch_events_since, latest_event_time


async def build_liquidation_forecast_summary(session: AsyncSession, *, symbol: str = "BTCUSDT") -> dict[str, Any]:
    settings = get_settings()
    if not settings.liquidation_intelligence_enabled:
        return {
            "status": "disabled",
            "symbol": symbol,
            "note": "Liquidation Intelligence collector is disabled (LIQUIDATION_INTELLIGENCE_ENABLED=false)",
        }

    now_ms = int(time.time() * 1000)
    since_ms = now_ms - WINDOW_MS["4h"]
    stale_after_ms = int(settings.liquidation_stale_after_seconds) * 1000
    bybit = await fetch_events_since(session, symbol=symbol, since_ms=since_ms, source="bybit")
    binance = await fetch_events_since(session, symbol=symbol, since_ms=since_ms, source="binance")
    statuses = await all_source_status(session)
    status_map = {s.source_exchange: s for s in statuses}
    collector = get_liquidation_collector()
    live = collector.status_payload() if collector else {}

    def _source_block(exchange: str, events: list, *, price_type: str, quality_label: str) -> dict[str, Any]:
        st = status_map.get(exchange)
        health = source_health_block(st, now_ms=now_ms, stale_after_ms=stale_after_ms, events_in_lookback=events)
        conn = health["connection_state"]
        if conn == "disconnected" and not events:
            return {
                **health,
                "status": "unavailable",
                "source_quality": quality_label,
                "windows": {},
                "price_clusters": [],
            }
        analytics = build_per_source_analytics(events, now_ms, source=exchange, price_type=price_type) if events else {}
        windows = (
            build_windows_with_coverage(events, now_ms, compute_window_metrics)
            if events
            else {k: {"coverage": "empty", "partial": False, "metrics": None} for k in ("5m", "15m", "1h", "4h")}
        )
        partial_any = any(w.get("partial") or w.get("coverage") in ("empty", "partial") for w in windows.values())
        if not events and conn in ("connected_idle", "connected_active", "connecting"):
            status = "connected_no_events"
        elif not events:
            status = "stale" if conn == "stale" else "unavailable"
        elif partial_any:
            status = "partial"
        else:
            status = "ok"
        return {
            **health,
            "status": status,
            "source_quality": quality_label,
            "windows": windows,
            "burst_metrics": analytics.get("burst_metrics", {}),
            "price_clusters": analytics.get("price_clusters", []),
        }

    bybit_block = _source_block("bybit", bybit, price_type="bankruptcy_price", quality_label="bybit_full_tape")
    binance_block = _source_block("binance", binance, price_type="avg_fill_price", quality_label="binance_sampled_stream")

    combined_events = bybit + binance
    combined_windows = (
        build_windows_with_coverage(combined_events, now_ms, compute_window_metrics) if combined_events else {}
    )
    combined_notional_1h = aggregate_observed_notional_total(combined_events, now_ms, WINDOW_MS["1h"]) if combined_events else None

    aggregate_status = "unavailable"
    if bybit_block.get("status") == "ok" or binance_block.get("status") == "ok":
        aggregate_status = "ok"
    elif bybit_block.get("status") in ("partial", "connected_no_events") or binance_block.get("status") in (
        "partial",
        "connected_no_events",
    ):
        aggregate_status = "partial"
    elif combined_events:
        aggregate_status = "partial"

    last_ev = await latest_event_time(session, symbol)

    return {
        "status": aggregate_status,
        "symbol": symbol,
        "sources": ["bybit", "binance"],
        "freshness_and_coverage": {
            "now_ms": now_ms,
            "lookback_ms": WINDOW_MS["4h"],
            "stale_after_seconds": settings.liquidation_stale_after_seconds,
            "bybit": {
                "connection_state": bybit_block.get("connection_state"),
                "event_freshness": bybit_block.get("event_freshness"),
            },
            "binance": {
                "connection_state": binance_block.get("connection_state"),
                "event_freshness": binance_block.get("event_freshness"),
            },
        },
        "source_quality": {
            "bybit": bybit_block.get("source_quality"),
            "binance": binance_block.get("source_quality"),
        },
        "coverage_notes": {
            "bybit": "Public allLiquidation stream; bankruptcy price labeled, not guaranteed fill",
            "binance": "forceOrder on /market/ws — sampled (~1 order / 1000ms), avg fill price; cumulative z deduped per order id when present",
            "aggregate": "Combined notional sums only — price clusters stay per-source; not complete market liquidation volume",
            "binance_dedupe": "Without order id, snapshot dedupe is best-effort only",
        },
        "bybit": bybit_block,
        "binance": binance_block,
        "combined_labeled": {
            "status": "ok" if combined_events else "unavailable",
            "total_observed_notional_usd_1h": combined_notional_1h,
            "windows": combined_windows,
            "note": "Dollar totals only — do not merge bankruptcy and fill prices into clusters",
        },
        "windows": combined_windows if combined_events else {},
        "price_clusters": {
            "bybit": bybit_block.get("price_clusters") or [],
            "binance": binance_block.get("price_clusters") or [],
        },
        "burst_metrics": bybit_block.get("burst_metrics") or binance_block.get("burst_metrics") or {},
        "last_event_time_ms": last_ev,
        "collector": live,
        "interpretation": "Observed liquidation events only — not predicted future liquidation zones",
    }
