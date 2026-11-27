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


def test_skipped_idea_still_renders_pattern_edges_and_analytical_path(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from matplotlib.axes import Axes
    from app.services.ai_ict_analysis import _chart_overlays_from_ai, _levels_for_chart
    from app.services.chart_renderer import render_ict_chart_png
    bars = example(.3,.8)
    patterns = pattern_context({"1h":bars},35*3600000)
    data = {"decision":"NO_TRADE","strategy_id":"trend_continuation",
            "_artifacts":{"strategy_persistence":{"trend_continuation":{"skipped":True}}},
            "verified_trade_plan":{"entry":110,"stop":103,"target":125},
            "analyst_summary":{"chart_patterns":patterns}}
    labels = []
    plot = Axes.plot
    def capture(self,*args,**kwargs):
        labels.append(kwargs.get("label"))
        return plot(self,*args,**kwargs)
    monkeypatch.setattr(Axes,"plot",capture)
    levels = _levels_for_chart(data)
    assert levels["analytical_only"]
    path = tmp_path/"analysis.png"
    render_ict_chart_png(output_path=path,symbol="BTCUSDT",interval="1h",candles=bars,
                         overlays=_chart_overlays_from_ai(data),live_price=115,decision="NO_TRADE",
                         trade_levels=levels,analyzed_at=datetime.now(timezone.utc))
    assert path.stat().st_size>1000
    assert "1h rising wedge" in labels and any("Possible path" in (x or "") for x in labels)


def test_hourly_watch_chart_uses_only_current_watch_zones():
    from app.services.ai_ict_analysis import _chart_overlays_from_ai, _levels_for_chart
    plan = dict(strategy="B",direction="SHORT",zone_low=110,zone_high=115,target=100)
    data = {"decision":"NO_TRADE","verified_trade_plan":{"entry":111,"stop":112,"target":109},
            "analyst_summary":{"hourly_watch":{"B:SHORT:1":plan}}}
    assert _levels_for_chart(data) is None
    assert _chart_overlays_from_ai(data)["hourly_watch"]["B:SHORT:1"] == plan
    data["analyst_summary"]["hourly_watch"] = {}
    assert _levels_for_chart(data) is None
