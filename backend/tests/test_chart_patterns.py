from math import cos, pi
from app.domain.candles import Candle
from app.services.chart_patterns import pattern_context


def example(upper_slope, lower_slope):
    bars = []
    for i in range(35):
        wave = cos(i*pi/4)
        ceiling = 140 + upper_slope*i
        floor = 80 + lower_slope*i
        mid = floor+(ceiling-floor)*(1+wave)/2
        bars.append(Candle(mid,mid+1,mid-1,mid,i*3600000))
    return bars


def test_wedge_and_channel_awareness_without_trade_plan():
    for slopes, kind in [((.3,.8),"rising_wedge"), ((-.3,-.3),"descending_channel"),
                         ((-.8,-.3),"falling_wedge")]:
        bars = example(*slopes)
        result = pattern_context({"1h":bars},35*3600000)
        assert any(p["kind"]==kind and p["position"]=="inside" for p in result)
        assert pattern_context({"1h":bars+[bars[-1]]},34*3600000)==pattern_context({"1h":bars[:-1]},34*3600000)
