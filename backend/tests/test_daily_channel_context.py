from datetime import datetime, timezone
from unittest.mock import patch
from app.services.daily_outlook_geometry import channel_context
from app.services.daily_outlook_image import chart_snapshot
from app.services.daily_outlook_schema import INSTRUCTIONS


def geometry():
    return dict(available=True, upper=110, lower=90, mid=100,
        timeframe="4h", method="major_pivot_envelope_v2", direction="bearish",
        historical=True, slope_per_day=-1, position="inside", upper_touches=3,
        lower_touches=2, end_time_ms=0)


def test_descriptive_context_is_not_a_trade_signal():
    c=channel_context(geometry(),105,"2026-11-05T04:00:00Z")
    assert c["position_in_width"]==0.75
    assert c["upper_at_snapshot"]==110 and c["lower_at_snapshot"]==90
    assert c["historical"] is True
    assert c["standalone_entry_signal"] is False
    assert "confirmed breakout" in c["interpretation"]
    outside=channel_context({**geometry(),"position":"above"},115,c["as_of"])
    assert outside["position_in_width"]>1
    assert outside["distance_to_upper_pct"]<0


def test_missing_channel_is_explicit():
    c=channel_context({"available":False,"reason":"insufficient_structure"},100,"now")
    assert not c["available"] and "upper_at_snapshot" not in c


def test_chart_reuses_saved_geometry_and_legacy_reports_still_compute():
    anchor=datetime(2026,11,5,4,tzinfo=timezone.utc)
    t=int(anchor.timestamp()*1000)-14400000
    ctx={"as_of":anchor.isoformat(),"live_price":105,"levels":[],
         "candles":{"4h":[[t,100,110,90,105]]},"chart_channel":geometry()}
    with patch("app.services.daily_outlook_image.pivot_channel",side_effect=AssertionError("recomputed")):
        assert chart_snapshot(ctx,{})["channel"]==ctx["chart_channel"]
    del ctx["chart_channel"]
    with patch("app.services.daily_outlook_image.pivot_channel",return_value={"available":False}) as fn:
        assert not chart_snapshot(ctx,{})["channel"]["available"]
        fn.assert_called_once()


def test_prompt_keeps_channel_secondary_and_non_executable():
    assert "secondary context" in INSTRUCTIONS
    assert "not executable Entry/SL/TP" in INSTRUCTIONS
