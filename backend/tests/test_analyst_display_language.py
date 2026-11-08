from app.services.analyst_outlook import (
    format_forecasts_fa,
    format_market_features_fa,
    safe_model_narrative_fa,
)


def test_mixed_armenian_forecast_reason_falls_back_to_candle_evidence():
    forecast = [{
        "scenario_type": "support_bounce",
        "direction": "LONG",
        "level": {
            "kind": "support", "timeframe": "15m",
            "low": 100.0, "high": 102.0,
            "status": "fresh", "touch_count": 0,
        },
        "target": {"kind": "resistance", "timeframe": "1h",
                   "low": 110.0, "high": 112.0},
        "reason_fa": "BTCUSDT اکنա fresh 15m աջակցություն",
        "condition_fa": "واکنش صعودی و تأیید کندل بستهٔ 5m لازم است.",
        "invalidation_fa": "تثبیت زیر کف حمایت، سناریو را بی‌اعتبار می‌کند.",
    }]
    text = format_forecasts_fa(forecast)
    assert "دست‌نخورده" in text
    assert "تعداد برخوردهای مجزا: 0" in text
    assert "BTCUSDT اکنա" not in text
    assert "Ա" not in text


def test_safe_model_narrative_rejects_armenian_but_keeps_persian():
    assert safe_model_narrative_fa("روند 4H نزولی است؛ RSI14 برابر 40.2 است.")
    assert safe_model_narrative_fa("BTCUSDT اکنա نزولی է") == ""


def test_indicator_summary_is_deterministic_persian_fallback():
    features = {"timeframes": {
        "4h": {"available": True, "ema9_ema20_relation": "below",
               "rsi14": 40.2, "adx14": 33.5, "atr14": 764.3,
               "relative_volume20": 0.10},
        "1h": {"available": True, "ema9_ema20_relation": "above",
               "rsi14": 50.8, "adx14": 28.7, "atr14": 184.5,
               "relative_volume20": 0.42},
    }}
    regime = {"timeframes": {
        "4h": {"trend": "bearish"},
        "1h": {"trend": "range"},
    }}
    text = format_market_features_fa(features, regime)
    assert "4H: ساختار نزولی" in text
    assert "1H: ساختار رنج" in text
    assert "EMA9 زیر EMA20" in text
    assert "RSI14=40.2" in text
    assert "ADX14=33.5" in text
    assert "RVOL20=0.1" in text
