from __future__ import annotations

import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.services.liquidation_intelligence.analytics import WINDOW_MS, build_exchange_analytics
from app.services.liquidation_intelligence.collector import get_liquidation_collector
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
    bybit = await fetch_events_since(session, symbol=symbol, since_ms=since_ms, source="bybit")
    binance = await fetch_events_since(session, symbol=symbol, since_ms=since_ms, source="binance")
    statuses = await all_source_status(session)
    status_map = {s.source_exchange: s for s in statuses}
    collector = get_liquidation_collector()
    live = collector.status_payload() if collector else {}

    def _source_block(exchange: str, events: list) -> dict[str, Any]:
        st = status_map.get(exchange)
        last_msg = int(st.last_message_at_ms or 0) if st else 0
        stale_after = int(settings.liquidation_stale_after_seconds) * 1000
        fresh = last_msg > 0 and (now_ms - last_msg) <= stale_after
        if not events and not fresh:
            return {
                "status": "unavailable",
                "source_quality": "no_data",
                "freshness": "unavailable",
            }
        analytics = build_exchange_analytics(events, now_ms)
        return {
            "status": "ok" if events else "stale",
            "freshness": "ok" if fresh else "stale",
            "source_quality": "bybit_full_tape" if exchange == "bybit" else "binance_sampled_stream",
            **analytics,
        }

    bybit_block = _source_block("bybit", bybit)
    binance_block = _source_block("binance", binance)

    combined_events = bybit + binance
    combined = build_exchange_analytics(combined_events, now_ms) if combined_events else {}
    aggregate_status = "ok" if combined_events else "unavailable"
    if bybit_block.get("status") == "unavailable" and binance_block.get("status") == "unavailable":
        aggregate_status = "unavailable"
    elif aggregate_status == "ok" and (
        bybit_block.get("freshness") == "stale" or binance_block.get("freshness") == "stale"
    ):
        aggregate_status = "partial"

    last_ev = await latest_event_time(session, symbol)

    return {
        "status": aggregate_status,
        "symbol": symbol,
        "sources": ["bybit", "binance"],
        "source_quality": {
            "bybit": bybit_block.get("source_quality"),
            "binance": binance_block.get("source_quality"),
        },
        "coverage_notes": {
            "bybit": "Public allLiquidation stream; bankruptcy price labeled, not guaranteed fill",
            "binance": "forceOrder stream samples at most one order per 1000ms — not a full tape",
            "aggregate": "Combined metrics are labeled sums; do not treat as complete market liquidation volume",
        },
        "bybit": bybit_block,
        "binance": binance_block,
        "combined_labeled": combined if combined_events else {"status": "unavailable"},
        "windows": combined.get("windows") if combined_events else {},
        "price_clusters": combined.get("price_clusters", []) if combined_events else [],
        "burst_metrics": combined.get("burst_metrics", {}) if combined_events else {},
        "last_event_time_ms": last_ev,
        "collector": live,
        "interpretation": "Observed liquidation events only — not predicted future liquidation zones",
    }
