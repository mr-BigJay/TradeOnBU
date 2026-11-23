from app.domain.candles import Candle
from app.services.breakout_boundaries import boundaries, line_price
from app.services.ict_confirmation_engine import _breakout_frame
from app.services.analyst_outlook import review_previous


def test_body_confirmation_priority_and_wicks():
    step = 300000
    bars = [Candle(99, 102, 96, 100, 0), Candle(100, 103, 98, 100, step),
            Candle(100, 105, 98, 104, 2*step)]
    assert _breakout_frame(bars, 99, 101, "LONG", 0) == "15m"
    assert _breakout_frame(bars[-1:], 99, 101, "LONG", 0) == "5m"
    assert _breakout_frame(bars[:2], 99, 101, "LONG", 0) is None
    assert _breakout_frame([Candle(101,102,95,97,step)],99,101,"SHORT",0) == "5m"


def test_converging_boundaries_expire_while_limit_is_still_inside_band():
    step = 3600000
    highs = [108,109,112,109,108,109,110,108,107]
    lows = [92,91,90,91,94,95,94,93,94,95]
    bars = [Candle(100,h,l,100,i*step) for i,(h,l) in enumerate(zip(highs+[107],lows))]
    lines = boundaries(bars,step,10*step,1)
    assert set(lines) == {"LONG","SHORT"}
    for z in lines.values():
        assert z["pattern"] == "triangle"
        assert abs(line_price(z,z["valid_until_ms"]) - z["price"]) <= 1


def test_previous_outlook_does_not_assign_order_of_events_inside_a_bar():
    scenario = dict(as_of_ms=0,valid_until_ms=600000,level={"low":99,"high":101},
                    target={"low":110,"high":112},scenario_type="support_bounce")
    previous = {"analyst_summary":{"forecast_scenarios":[scenario]}}
    bars = [Candle(103,111,98,104,0), Candle(104,108,103,106,300000)]
    assert "ثبت نشد" in review_previous(previous,bars,600000)["status_fa"]
