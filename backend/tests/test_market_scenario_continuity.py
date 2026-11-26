from app.domain.candles import Candle
from app.services.market_scenario import STEP, review


def test_scenario_survives_intrabar_breach_until_four_hour_close():
    old = {"direction":"SHORT","level":110,"as_of_ms":STEP}
    previous = {"analyst_summary":{"market_scenario":old}}
    bars = [Candle(100,115,98,105,STEP)]
    scenario = review(previous,bars,[],None,2*STEP)
    assert (scenario["status"],scenario["direction"],scenario["level"]) == ("continued","SHORT",110)
    bars.append(Candle(105,115,104,112,2*STEP))
    updated = review({"analyst_summary":{"market_scenario":scenario}},bars,[],None,3*STEP)
    assert updated["broken"]["close"] == 112
    assert updated["broken"]["level"] == 110
