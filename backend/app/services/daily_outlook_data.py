"""Closed-bar multi-day context. Daily/weekly bars are aggregated in UTC."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from collections import defaultdict

from app.domain.candles import Candle
from app.services.market_data import MarketDataService

HOUR = 3_600_000
DAY = 24 * HOUR


def aggregate_complete(bars: list[Candle], *, days: int, now_ms: int) -> list[Candle]:
    step = 4 * HOUR if days == 1 else DAY
    width = days * DAY
    # Unix epoch is Thursday; weekly candles start Monday UTC.
    offset = 0 if days == 1 else 4 * DAY
    groups: dict[int, dict[int, Candle]] = defaultdict(dict)
    for bar in bars:
        start = ((bar.time_ms - offset) // width) * width + offset
        groups[start][bar.time_ms] = bar
    result = []
    for start, group in sorted(groups.items()):
        expected = list(range(start, start + width, step))
        if start + width > now_ms or sorted(group) != expected:
            continue
        rows = [group[t] for t in expected]
        result.append(Candle(
            time_ms=start, open=rows[0].open, high=max(b.high for b in rows),
            low=min(b.low for b in rows), close=rows[-1].close,
            volume=sum(b.volume for b in rows) if all(b.volume is not None for b in rows) else None,
        ))
    return result


def validate_bars(bars: list[Candle], *, step: int, now_ms: int, minimum: int) -> None:
    if len(bars) < minimum:
        raise ValueError("insufficient_market_history")
    expected_last = (now_ms // step) * step - step
    if bars[-1].time_ms != expected_last:
        raise ValueError("latest_closed_candle_missing")
    for i, bar in enumerate(bars):
        prices = [bar.open, bar.high, bar.low, bar.close]
        if not all(math.isfinite(p) and p > 0 for p in prices):
            raise ValueError("invalid_market_prices")
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close) or bar.low > bar.high:
            raise ValueError("invalid_candle_range")
        if bar.time_ms % step or (i and bar.time_ms - bars[i - 1].time_ms != step):
            raise ValueError("market_history_gap")


def level_context(daily: list[Candle], weekly: list[Candle], four_hour: list[Candle]) -> list[dict]:
    candidates = [
        ("previous_day_high", "سقف روز کامل قبل", daily[-1].high),
        ("previous_day_low", "کف روز کامل قبل", daily[-1].low),
        ("previous_week_high", "سقف هفته کامل قبل", weekly[-1].high),
        ("previous_week_low", "کف هفته کامل قبل", weekly[-1].low),
        ("range_20d_high", "سقف ۲۰ روز کامل", max(b.high for b in daily[-20:])),
        ("range_20d_low", "کف ۲۰ روز کامل", min(b.low for b in daily[-20:])),
        ("range_5d_high", "سقف ۵ روز کامل", max(b.high for b in daily[-5:])),
        ("range_5d_low", "کف ۵ روز کامل", min(b.low for b in daily[-5:])),
        ("range_4h_12_high", "سقف ۱۲ کندل ۴ساعته", max(b.high for b in four_hour[-12:])),
        ("range_4h_12_low", "کف ۱۲ کندل ۴ساعته", min(b.low for b in four_hour[-12:])),
    ]
    return [{"id": key, "label_fa": label, "price": price} for key, label, price in candidates]


async def build_daily_context(symbol: str, market: MarketDataService | None = None) -> dict:
    market = market or MarketDataService()
    now_ms = market._now_ms()
    start = (now_ms // DAY) * DAY - 90 * DAY
    bundle = await market.fetch_historical_closed(symbol, "4h", from_ms=start, max_bars=600)
    if not bundle.data_complete:
        raise ValueError("incomplete_4h_history")
    bars = sorted(bundle.closed, key=lambda b: b.time_ms)
    validate_bars(bars, step=4 * HOUR, now_ms=now_ms, minimum=80 * 6)
    daily = aggregate_complete(bars, days=1, now_ms=now_ms)
    weekly = aggregate_complete(daily, days=7, now_ms=now_ms)
    validate_bars(daily, step=DAY, now_ms=now_ms, minimum=80)
    if len(weekly) < 10:
        raise ValueError("insufficient_weekly_history")
    quote = await market.fetch_live_price(symbol)
    if market.is_quote_stale(quote) or not math.isfinite(quote.price) or quote.price <= 0:
        raise ValueError("stale_live_quote")

    def compact(rows: list[Candle]) -> list[list]:
        return [[b.time_ms, b.open, b.high, b.low, b.close, b.volume] for b in rows]

    trs = [max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
           for a, b in zip(daily[-15:-1], daily[-14:])]
    return {
        "symbol": symbol, "as_of": datetime.now(timezone.utc).isoformat(),
        "source": "Bitunix futures; 1D/1W aggregated from complete 4h candles, UTC Monday weeks",
        "horizon_days": [3, 7], "live_price": quote.price,
        "columns": ["open_time_ms", "open", "high", "low", "close", "volume"],
        "candles": {"1w": compact(weekly[-12:]), "1d": compact(daily[-80:]), "4h": compact(bars[-120:])},
        "levels": level_context(daily, weekly, bars),
        "daily_atr14": sum(trs) / len(trs),
        "news_and_calendar": "unavailable_no_external_news_feed",
    }
