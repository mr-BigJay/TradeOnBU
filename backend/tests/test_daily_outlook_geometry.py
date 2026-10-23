import math
from datetime import datetime, timezone
import pytest
from app.services.daily_outlook_geometry import nearby_levels, pivot_channel

ANCHOR = datetime(2026,10,23,tzinfo=timezone.utc)

def bars(slope=2):
    end = int(ANCHOR.timestamp()*1000)
    result = []
    for i in range(60):
        close = 1000+slope*i+20*math.sin(i*math.pi/6)
        result.append([end-(60-i)*14400000,close,close+4,close-4,close])
    return result

def test_nearby_levels_sorted_deduplicated_and_equal_excluded():
    levels = [{"id":str(v),"price":v} for v in [100,99,98.95,98,97,96,101,102,103,104]]
    result = nearby_levels(levels,100)
    assert [l["price"] for l in result["support"]] == [99,98,97]
    assert [l["code"] for l in result["resistance"]] == ["R1","R2","R3"]
    assert result["resistance"][0]["price"] == 101

@pytest.mark.parametrize("slope,direction", [(2,"bullish"),(-2,"bearish"),(0,"range")])
def test_parallel_confirmed_pivots(slope,direction):
    rows = bars(slope)
    result = pivot_channel(rows,ANCHOR,14400,rows[-1][4])
    assert result["available"] and result["direction"] == direction
    assert result["upper_touches"] >= 2 and result["lower_touches"] >= 2
    assert result["lower"] < result["mid"] < result["upper"]
    assert result["close_coverage"] >= .85

def test_channel_absent_for_insufficient_gap_or_breakout():
    rows = bars()
    assert not pivot_channel(rows[:20],ANCHOR,14400,1000)["available"]
    assert pivot_channel(rows[:-3]+rows[-2:],ANCHOR,14400,1100)["reason"] == "history_gap"
    assert pivot_channel(rows,ANCHOR,14400,1500)["reason"] == "broken_channel"

def test_monotonic_price_does_not_invent_pivot_channel():
    rows = bars()
    for i,row in enumerate(rows):
        row[1:] = [1000+i,1005+i,995+i,1000+i]
    assert not pivot_channel(rows,ANCHOR,14400,1060)["available"]
