import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.domain.candles import Candle
from app.services.long_term_context import DAY, load_context


def test_closed_months_weeks_and_cache_across_leap_day():
    now = int(datetime(2024, 3, 1, 8, tzinfo=timezone.utc).timestamp()*1000)
    step = DAY // 6
    start = int(datetime(2023, 2, 20, tzinfo=timezone.utc).timestamp()*1000)
    bars = [Candle(100, 103, 98, 101, t) for t in range(start, now, step)]
    market = SimpleNamespace(fetch_historical_closed=AsyncMock(
        return_value=SimpleNamespace(data_complete=True, closed=bars)))
    result = asyncio.run(load_context(market, "BTCUSDT", now, None))
    assert result["status"] == "complete"
    assert len(result["1w"]) == 52 and len(result["1M"]) == 12
    feb = result["1M"][-1]
    assert feb == [now - 8*3600000 - 29*DAY, 100, 103, 98, 101]
    assert result["1w"][-1][0] + 7*DAY == now - 8*3600000 - 4*DAY
    previous = {"analyst_summary": {"long_term_context": result}}
    assert asyncio.run(load_context(market, "BTCUSDT", now + step, previous)) == result
    market.fetch_historical_closed.assert_awaited_once()
    market.fetch_historical_closed.side_effect = RuntimeError("offline")
    assert asyncio.run(load_context(market, "BTCUSDT", now + 7*DAY, previous))["status"] == "unavailable"
