from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.config import Settings, get_effective_settings
from app.domain.candles import Candle
from app.services.market_data import MarketDataService

AI_TIMEFRAMES = ("4h", "1h", "15m", "5m", "1m")
MIN_CANDLES = 20


class MarketContextError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _limits(settings: Settings) -> dict[str, int]:
    return {
        "4h": settings.ai_candle_limit_4h,
        "1h": settings.ai_candle_limit_1h,
        "15m": settings.ai_candle_limit_15m,
        "5m": settings.ai_candle_limit_5m,
        "1m": settings.ai_candle_limit_1m,
    }


def serialize_candle(c: Candle) -> dict[str, Any]:
    row: dict[str, Any] = {
        "time_ms": c.time_ms,
        "time_iso": datetime.fromtimestamp(c.time_ms / 1000.0, tz=timezone.utc).isoformat(),
        "open": c.open,
        "high": c.high,
        "low": c.low,
        "close": c.close,
    }
    if c.volume is not None:
        row["volume"] = c.volume
    return row


async def build_raw_market_payload(symbol: str, settings: Settings | None = None) -> tuple[dict[str, Any], float, dict[str, list[Candle]]]:
    settings = settings or get_effective_settings()
    market = MarketDataService(settings)
    limits = _limits(settings)
    payload: dict[str, Any] = {
        "symbol": symbol,
        "paper_only": True,
        "data_policy": "raw_ohlcv_only_no_rule_engine_conclusions",
        "timeframes": {},
        "incomplete_intervals": [],
    }
    candles_by_interval: dict[str, list[Candle]] = {}

    for interval in AI_TIMEFRAMES:
        limit = limits[interval]
        try:
            bundle = await market.fetch_kline_bundle(symbol, interval, limit)
        except Exception as exc:
            raise MarketContextError("market_fetch_failed", f"{interval}: {exc}") from exc

        closed = bundle.closed[-limit:]
        candles_by_interval[interval] = closed
        complete = bundle.data_complete and len(closed) >= MIN_CANDLES
        payload["timeframes"][interval] = {
            "interval": interval,
            "requested_limit": limit,
            "closed_count": len(closed),
            "data_complete": bundle.data_complete,
            "gaps_detected": len(bundle.gaps_detected or []),
            "gaps_remaining": len(bundle.gaps_remaining or []),
            "usable_for_ai": complete,
            "candles": [serialize_candle(c) for c in closed],
        }
        if not complete:
            payload["incomplete_intervals"].append(interval)

    quote = await market.fetch_live_price(symbol)
    stale = market.is_quote_stale(quote)
    payload["live_price"] = quote.price
    payload["quote_fetched_at"] = quote.fetched_at.isoformat()
    payload["quote_stale"] = stale
    payload["quote_source"] = quote.source
    if stale:
        raise MarketContextError("stale_live_price", "Live Bitunix quote is stale or unavailable")

    return payload, quote.price, candles_by_interval
