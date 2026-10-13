from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.config import Settings, get_effective_settings
from app.domain.candles import Candle
from app.services.market_data import MarketDataService

AI_TIMEFRAMES = ("4h", "1h", "15m", "5m", "1m")
MIN_CANDLES = 20
# Trim order when over budget: drop oldest bars from finest TF first.
TRIM_ORDER = ("1m", "5m", "15m", "1h", "4h")


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


def _estimate_json_chars(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False))


def _apply_token_budget(payload: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Reduce oldest candles (finest TF first) until payload fits max_chars."""
    budget_meta: dict[str, Any] = {
        "max_json_chars": max_chars,
        "estimated_chars_before": _estimate_json_chars(payload),
        "trimmed": [],
    }
    if budget_meta["estimated_chars_before"] <= max_chars:
        budget_meta["estimated_chars_after"] = budget_meta["estimated_chars_before"]
        payload["token_budget"] = budget_meta
        return payload

    timeframes = payload.get("timeframes") or {}
    for interval in TRIM_ORDER:
        tf = timeframes.get(interval)
        if not tf or not tf.get("candles"):
            continue
        while tf["candles"] and _estimate_json_chars(payload) > max_chars:
            tf["candles"].pop(0)
            tf["closed_count"] = len(tf["candles"])
            budget_meta["trimmed"].append(interval)
        if _estimate_json_chars(payload) <= max_chars:
            break

    budget_meta["estimated_chars_after"] = _estimate_json_chars(payload)
    budget_meta["approx_tokens"] = budget_meta["estimated_chars_after"] // 4
    budget_meta["note"] = (
        "Delivered history may be trimmed from configured limits to respect ai_prompt_max_json_chars. "
        "Check per-interval closed_count vs requested_limit."
    )
    payload["token_budget"] = budget_meta
    return payload


async def build_raw_market_payload(
    symbol: str,
    settings: Settings | None = None,
    *,
    market: MarketDataService | None = None,
) -> tuple[dict[str, Any], float, dict[str, list[Candle]]]:
    settings = settings or get_effective_settings()
    market = market or MarketDataService(settings)
    limits = _limits(settings)
    payload: dict[str, Any] = {
        "symbol": symbol,
        "paper_only": True,
        "data_policy": "raw_ohlcv_only_no_rule_engine_conclusions",
        "timeframes": {},
        "incomplete_intervals": [],
        "delivery_limits": limits,
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

    payload = _apply_token_budget(payload, settings.ai_prompt_max_json_chars)
    return payload, quote.price, candles_by_interval
