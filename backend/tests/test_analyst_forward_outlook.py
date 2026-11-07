"""Four-hour evidence-bound outlook and measured prompt budget regressions.

These tests exercise Paper-only reporting. Forecasts cannot create executable plans.
"""
from copy import deepcopy
from datetime import datetime, timezone
from app.services import multi_setup_analyst as analyst
from app.services.analyst_outlook import compile_forecasts, format_forecasts_fa
from app.services.analyst_payload import build_analyst_payload
from app.services.analyst_regime import executable_regime
from tests.test_multi_setup_analyst import fixture


def test_bounce_and_break_scenarios_survive_zero_executable_plans():
    series, costs, evidence, now = fixture()
    evidence = deepcopy(evidence)
    evidence.append(dict(id="lower", kind="support", timeframe="1h",
                         time_ms=now-3600000, low=999., high=1001., price=1000.,
                         direction=None, status="retested", touch_count=3))
    evidence[0].update(status="fresh", touch_count=0)
    evidence[1].update(status="retested", touch_count=3)
    raw = {
        "market_summary_fa": "روند مشروط است",
        "plans": [],
        "forecast_scenarios": [
            {"scenario_type": "support_bounce", "direction": "LONG",
             "level_id": "stop", "target_id": "target",
             "reason_fa": "حمایت چندبار آزموده شده و بازگشت تنها بعد از تأیید قابل بررسی است"},
            {"scenario_type": "support_break", "direction": "SHORT",
             "level_id": "stop", "target_id": "lower",
             "reason_fa": "اگر حمایت زیر فشار فروش شکسته شود، بازآزمایی لازم است"},
            {"scenario_type": "resistance_rejection", "direction": "SHORT",
             "level_id": "zone", "target_id": "stop",
             "reason_fa": "مقاومت هنوز لمس نشده است؛ هیچ تضمینی برای واکنش نیست"},
        ],
    }
    report = analyst.compile_response(
        raw, evidence, series, costs, now, now,
        {sid: True for sid in analyst.PROFILES}
    )
    assert report["setup_mode"] == "flat"
    assert report["decision"] == "NO_TRADE"
    assert report.get("strategy_id") is None
    assert report["strategy_candidates"] == {}
    forecast = report["analyst_summary"]["forecast_scenarios"]
    assert len(forecast) == 3
    assert all(p["watchlist_only"] and not p["paper_order"] for p in forecast)
    assert forecast[0]["level"]["touch_count"] == 3
    assert forecast[1]["target"]["id"] == "lower"
    assert forecast[2]["level"]["status"] == "fresh"
    text = format_forecasts_fa(forecast)
    assert "شکست و بازآزمایی حمایت" in text
    assert "تعداد برخوردهای مجزا: 3" in text
    assert "ناحیه 1017 تا 1019" in text
    assert "مقصد مشروط: 1060 تا 1062" in text
    assert "هیچ سناریوی تحت نظر" in text


def test_forecasts_reject_made_up_levels_wrong_side_and_duplicates():
    _, _, evidence, _ = fixture()
    proposals = [
        {"scenario_type": "support_break", "direction": "SHORT",
         "level_id": "invented", "target_id": None, "reason_fa": "x"},
        {"scenario_type": "resistance_rejection", "direction": "SHORT",
         "level_id": "zone", "target_id": "target", "reason_fa": "x"},
        {"scenario_type": "support_bounce", "direction": "LONG",
         "level_id": "stop", "target_id": "target", "reason_fa": "x"},
        {"scenario_type": "support_bounce", "direction": "LONG",
         "level_id": "stop", "target_id": "target", "reason_fa": "x"},
    ]
    accepted, rejected = compile_forecasts(proposals, evidence)
    assert len(accepted) == 1
    assert rejected == [
        "1:unknown_or_wrong_level",
        "2:wrong_target_kind",
        "4:duplicate_forecast",
    ]


def test_support_and_resistance_retests_count_distinct_post_confirmation_events(monkeypatch):
    series, _, _, now = fixture()
    interval = analyst.STEPS["5m"]
    origin = series["5m"][5].time_ms
    levels = {
        "support_resistance": [
            dict(timeframe="5m", time_ms=origin, kind="support", price=1000.),
            dict(timeframe="5m", time_ms=origin, kind="resistance", price=2500.),
        ],
        "order_blocks": [],
    }
    monkeypatch.setattr(analyst, "market_zones", lambda _: levels)
    catalog = analyst.catalog(series, now)
    support = next(z for z in catalog if z["kind"] == "support")
    resistance = next(z for z in catalog if z["kind"] == "resistance")
    confirmed = origin + 3*interval
    assert support["confirmed_ms"] == confirmed
    assert resistance["confirmed_ms"] == confirmed
    assert resistance["status"] == "fresh" and resistance["touch_count"] == 0
    assert support["status"] == "retested" and support["touch_count"] >= 1
    assert support["last_touch_ms"] is not None
    touches = [b for b in series["5m"] if b.time_ms >= confirmed
               and b.low <= support["high"] and b.high >= support["low"]]
    assert support["touch_count"] <= len(touches)


def test_payload_measures_all_sections_instruction_cost_and_micro_history():
    series, _, evidence, _ = fixture()
    market = {"symbol": "BTCUSDT", "timeframes": {
        tf: {"candles": [{"time_ms": b.time_ms, "time_iso": datetime.fromtimestamp(
            b.time_ms/1000, timezone.utc).isoformat(), "open": b.open, "high": b.high,
            "low": b.low, "close": b.close, "volume": b.volume} for b in bars],
             "closed_count": len(bars)}
        for tf, bars in series.items()
    }}
    original = deepcopy(market)
    intelligence = {"status": "available", "sources": {"funding": {
        "source": "bitunix", "funding_rate": 0.0001
    }}}
    inp, audit = build_analyst_payload(
        market=market, evidence_catalog=evidence, market_intelligence=intelligence,
        strategy_features={"timeframes": {"4h": {"atr14": 10.}, "5m": {"rsi14": 55.}}},
        enabled_strategies={"ict_reversal": True},
        execution_regime=executable_regime(series),
        instructions="a short instruction",
    )
    import json
    data = json.loads(inp)
    assert market == original
    assert audit["instructions_chars"] == 19
    assert audit["request_text_chars"] == len(inp) + 19
    assert audit["sections"]["market_intelligence"]["after_chars"] > 0
    assert audit["breakdown"]["market_intelligence_by_source"]["funding"]["json_chars"] > 0
    assert audit["breakdown"]["strategy_features_by_tf"]["4h"]["json_chars"] > 0
    assert audit["breakdown"]["evidence_catalog_by_kind_tf"]["resistance:5m"]["count"] == 1
    assert audit["candle_counts"]["1m"] == 48
    assert audit["sent_candle_counts"]["1m"] == 45
    assert audit["sent_candle_counts"]["5m"] == 48
    assert data["market"]["timeframes"]["1m"]["full_server_closed_count"] == 48
    assert data["market"]["timeframes"]["4h"]["closed_count"] == 48
    assert audit["saved_chars"] > 0
    assert "not exact token attribution" in audit["accounting_note"]
