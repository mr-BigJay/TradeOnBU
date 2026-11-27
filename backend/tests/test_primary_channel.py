from types import SimpleNamespace

from app.domain.candles import Candle
from app.services.market_scenario import STEP, broad_channel, channel_cutoff, hourly_break_watch
from app.services.pattern_memory import update_drawings


def test_primary_channel_survives_hourly_exit_then_requires_new_anchors():
    def candle(index, price):
        return Candle(price, price + 2, price - 2, price, index * STEP)

    bars = [candle(i, 290 - i / 2) for i in range(120)]
    swings = [SimpleNamespace(kind=kind, index=i, time_ms=i * STEP,
                              price=(300 if kind == "HIGH" else 280) - i / 2)
              for i, kind in [(4,"HIGH"), (12,"LOW"), (50,"HIGH"), (80,"LOW"),
                              (96,"HIGH"), (110,"LOW")]]
    channel = broad_channel(bars, swings, daily="bearish")
    assert channel["upper"]["anchors"][0][0] == 4 * STEP
    original = {side: channel[side]["anchors"] for side in ("upper","lower")}
    hour = STEP // 4
    outside = Candle(250, 252, 247, 250, 120 * STEP)
    hourly_break_watch(channel, [outside], 120 * STEP + hour)
    assert "awaiting 4h" in channel["break_status"]

    def replay(active, data):
        previous = {"analyst_summary": {"drawing_memory": {"active": active}}}
        return update_drawings(previous, [], {"4h": data}, data[-1].time_ms + STEP)

    bars.append(candle(120, 230))
    follow = replay([channel], bars)
    assert "break_status" not in follow["active"][0]
    assert all(follow["active"][0][side]["anchors"] == original[side] for side in original)
    bars.append(candle(121, 260))
    broken = replay(follow["active"], bars)
    assert not broken["active"] and broken["ended"][0]["position"] == "above"
    cutoff = channel_cutoff(broken)
    assert cutoff == 122 * STEP
    assert broad_channel(bars, swings, since=cutoff) is None
    new_bars = [candle(i, 260 + (i-122)/2) for i in range(122,185)]
    new_swings = [SimpleNamespace(kind=k, index=i,time_ms=i*STEP,
                  price=(250 if k=="LOW" else 270)+(i-122)/2)
                  for i,k in [(123,"LOW"),(129,"HIGH"),(161,"LOW"),(172,"HIGH")]]
    replacement = broad_channel(bars + new_bars, swings + new_swings, since=cutoff)
    assert replacement["kind"] == "ascending_channel"
    assert all(replacement[s]["anchors"][0][0] >= cutoff for s in original)


def test_channel_allows_isolated_formation_excursion_but_rejects_later_break():
    def candle(i, offset):
        price = 300 - i / 2 + offset
        return Candle(price, price + 1, price - 1, price, i * STEP)

    bars = [candle(i, -10) for i in range(48)]
    swings = [SimpleNamespace(index=i, time_ms=i*STEP, kind=kind,
                              price=300-i/2-(20 if kind == "LOW" else 0))
              for i, kind in [(1,"HIGH"), (8,"LOW"), (20,"LOW"), (32,"HIGH")]]
    bars[12] = candle(12, 6)
    assert broad_channel(bars, swings)["kind"] == "descending_channel"
    bars[37] = candle(37, 6)
    assert broad_channel(bars, swings) is None
    bars[37] = candle(37, -10)
    for i in range(12, 20):
        bars[i] = candle(i, 6)
    assert broad_channel(bars, swings) is None
