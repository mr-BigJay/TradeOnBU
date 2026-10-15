"""Preconditions for scheduled ICT analysis (closed 1H candle, fresh data)."""

from __future__ import annotations

from app.config import get_effective_settings
from app.domain.candle_utils import candle_close_ms
from app.domain.market_freshness import is_market_data_fresh
from app.services.market_data import MarketDataService

INTERVAL_1H = "1h"


async def scheduled_analysis_market_ready(
    market: MarketDataService,
    *,
    symbol: str | None = None,
) -> tuple[bool, str | None]:
    settings = get_effective_settings()
    symbol = symbol or settings.default_symbol
    now_ms = market._now_ms()
    bundle = await market.fetch_kline_bundle(symbol, INTERVAL_1H, limit=8)
    if not bundle.closed:
        return False, "no_closed_1h_candle"
    if not bundle.data_complete:
        return False, "incomplete_1h_history"
    last = bundle.closed[-1]
    close_ms = candle_close_ms(last, INTERVAL_1H)
    if now_ms < close_ms:
        return False, "forming_1h_candle"
    fresh, _, _ = is_market_data_fresh(
        last,
        INTERVAL_1H,
        now_ms,
        settings.market_data_stale_seconds,
    )
    if not fresh:
        return False, "stale_1h_market_data"
    return True, None
