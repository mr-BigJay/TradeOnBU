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
    assert pivot_channel(rows,ANCHOR,14400,1500)["position"] == "above"

def test_monotonic_price_does_not_invent_pivot_channel():
    rows = bars()
    for i,row in enumerate(rows):
        row[1:] = [1000+i,1005+i,995+i,1000+i]
    assert not pivot_channel(rows,ANCHOR,14400,1060)["available"]


def test_historical_channel_survives_later_breakout_and_daily_is_not_used():
    rows = bars()
    old_end = rows[-1][0]
    for i in range(20):
        c = 1500+i*60
        rows.append([old_end+(i+1)*14400000,c,c+4,c-4,c])
    result = pivot_channel(rows,ANCHOR,14400,2640)
    assert result["available"] and result["historical"]
    assert result["position"] == "above"
    assert result["end_time_ms"] <= rows[60][0]
    assert not pivot_channel(rows,ANCHOR,86400,2640)["available"]


def test_broad_structure_beats_recent_narrow_internal_channel():
    end = int(ANCHOR.timestamp()*1000)
    rows = []
    for i in range(120):
        wave = 50*math.sin(i*math.pi/12) if i < 80 else 8*math.sin(i*math.pi/6)
        c = 2000-i+wave
        rows.append([end-(120-i)*14400000,c,c+4,c-4,c])
    result = pivot_channel(rows,ANCHOR,14400,rows[-1][4])
    assert result["available"] and result["bars"] >= 90
    assert result["upper"]-result["lower"] >= 95
    assert result["slope_per_day"] == pytest.approx(-6,abs=.5)
    assert result["candidates_evaluated"] > 1
    assert result["wick_coverage"] >= .95


def test_repeated_outer_wicks_set_bounds_not_mean_of_internal_pivots():
    rows = bars(0)
    for i in range(len(rows)):
        if i % 12 == 3:
            rows[i][2] += 20
        if i % 12 == 9:
            rows[i][3] -= 20
    result = pivot_channel(rows,ANCHOR,14400,1000)
    assert result["available"]
    assert result["upper"] >= 1044-1e-6
    assert result["lower"] <= 956+1e-6
    # At least one line goes through two actual major wick anchors.
    side = 2 if result["anchor_side"] == "upper" else 3
    bound = result["upper"] if side == 2 else result["lower"]
    for t in result["anchor_times_ms"]:
        row = next(r for r in rows if r[0] == t)
        x = (t/1000-ANCHOR.timestamp())/86400
        assert bound+result["slope_per_day"]*x == pytest.approx(row[side])


def test_channel_is_deterministic_without_changing_input():
    import copy
    rows = bars()
    original = copy.deepcopy(rows)
    one = pivot_channel(rows,ANCHOR,14400,rows[-1][4])
    two = pivot_channel(rows,ANCHOR,14400,rows[-1][4])
    assert one == two and rows == original
