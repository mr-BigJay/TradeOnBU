from __future__ import annotations

from app.domain.candles import Candle
from app.domain.market_freshness import is_market_data_fresh, max_stale_age_seconds


def test_freshness_uses_close_time_not_open():
    interval = "5m"
    open_ms = 1_000_000_000_000
    bar = Candle(time_ms=open_ms, open=1, high=2, low=0.5, close=1.5, volume=1)
    # 4 minutes after open — bar not closed yet; last closed would be previous
    # Simulate last CLOSED bar: open at open_ms, now 4m after open => age since close still within 5m+grace when using close
    now_ms = open_ms + 4 * 60_000
    fresh, age, allowed = is_market_data_fresh(bar, interval, now_ms, configured_stale_seconds=120)
    assert allowed >= 330
    assert age == max(0, (now_ms - (open_ms + 300_000)) // 1000)


def test_freshness_ok_mid_interval():
    interval = "5m"
    open_ms = 1_700_000_000_000
    bar = Candle(time_ms=open_ms, open=100, high=101, low=99, close=100, volume=1)
    now_ms = open_ms + 240_000  # 4 min after open, 1 min before close
    fresh, _, allowed = is_market_data_fresh(bar, interval, now_ms, configured_stale_seconds=120)
    assert allowed == max_stale_age_seconds(interval, 120)
    assert fresh is True
