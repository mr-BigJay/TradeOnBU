"""Only the presentation of analyst results is shortened; never change paper plans."""
from copy import deepcopy

from app.services.analyst_brief_report import compact_analyst_report_fa
from app.services.ai_trader_report import is_report_clean


def _plan(strategy, direction="LONG"):
    return dict(
        strategy_id=strategy, setup_mode="conditional",
        potential_direction=direction, analyst_rank=1,
        entry_zone_low=61277.677142857145,
        entry_zone_high=61483.92285714286,
        entry_price=61380.8,
        stop_loss=61120.6, take_profit=61924.5,
        trade_against_dominant_trend=True,
        confirmation_required=(
            "Sweep و بازپس‌گیری معتبر سطح 61256.4 با کندل بستهٔ 5m؛ "
            "ثبت سفارش Limit برای پولبک بعدی."
        ),
    )


def _structured():
    base = _plan("ict_reversal")
    base.update(
        analyst_summary={
            "execution_regime": {
                "timeframes": {
                    "4h": {"trend": "bearish"},
                    "1h": {"trend": "bearish"},
                }
            },
            "market_summary_fa": "این متن طولانی است. " * 90,
            "comparison_fa": "جملات اضافی و تکراری دربارهٔ بازار. " * 90,
            "outlook_major_levels": [
                {"role": "resistance", "timeframe": "4h", "low": 63906.1, "high": 64205.9},
                {"role": "resistance", "timeframe": "4h", "low": 65525.1, "high": 65824.9},
                {"role": "support", "timeframe": "1h", "low": 61380.8, "high": 61500.0},
            ],
            "forecast_scenarios": [{
                "scenario_type": "resistance_rejection",
                "direction": "SHORT",
                "level": {"low": 63906.1, "high": 64205.9},
                "target": None,
                "reason_fa": "شواهد تفصیلی بسیار طولانی " * 60,
            }],
            "execution_fallbacks": [],
        },
        strategy_candidates={},
        _artifacts={
            "strategy_features": {"timeframes": {"4h": {"ema9": 62000, "rsi14": 32}}},
            "strategy_persistence": {"ict_reversal": {"created": True, "setup_id": "fake-uuid"}},
        },
    )
    return base


def test_compact_fa_keeps_operational_levels_and_trend_without_verbose_narratives():
    report_data = _structured()
    original = deepcopy(report_data)
    text = compact_analyst_report_fa(report_data, live=61766.7)

    assert "4H نزولی · 1H نزولی" in text
    assert "61,766.7" in text
    assert "سطوح اصلی:" in text
    assert "63,906.1 تا 64,205.9" in text
    assert "چشم‌انداز مشروط چهار‌ساعته" in text
    assert "بدون مقصد ساختاری معتبر" in text
    assert "A · برگشت ICT: پلن مشروط LONG" in text
    assert "B · ادامه‌روند: بدون پلن اجرایی" in text
    assert "C · شکست و بازآزمایی: بدون پلن اجرایی" in text
    assert "61,277.7 تا 61,483.9" in text
    assert "SL: 61,120.6 | TP: 61,924.5" in text
    assert "Sweep و بازپس‌گیری معتبر" in text
    assert "خلاف روند غالب" in text
    assert "هنوز سفارش یا پوزیشن بازشده نیستند" in text
    for forbidden in ("ADX14", "EMA9", "RSI14", "جملات اضافی", "fake-uuid"):
        assert forbidden not in text
    assert len(text) < 1300
    assert is_report_clean(text)
    assert report_data == original  # strictly display-only


def test_compact_fa_marks_server_sourced_B_and_C_without_fabricating_orders():
    data = _structured()
    b = _plan("trend_continuation", direction="SHORT")
    b.update(
        analyst_rank=2, trade_against_dominant_trend=False,
        confirmation_required="پولبک معتبر به مقاومت 4H",
        stop_loss=62000, take_profit=60800,
    )
    c = _plan("breakout_retest")
    c.update(analyst_rank=3, trade_against_dominant_trend=False)
    data["strategy_candidates"] = {
        "trend_continuation": b, "breakout_retest": c,
    }
    data["analyst_summary"]["execution_fallbacks"] = [
        {"strategy_id": "trend_continuation"}, {"strategy_id": "breakout_retest"}
    ]
    text = compact_analyst_report_fa(data, live=61766.7)
    assert "B · ادامه‌روند: پلن مشروط SHORT (ساختاریِ سرور)" in text
    assert "C · شکست و بازآزمایی: پلن مشروط LONG (ساختاریِ سرور)" in text
    assert text.index("A · برگشت ICT | LONG") < text.index("B · ادامه‌روند | SHORT")
    assert "B: امکان ثبت Limit" in text
    assert len(text) < 1750


def test_compact_fa_empty_analysis_does_not_invent_levels_or_position():
    data = {
        "analyst_summary": {
            "execution_regime": {"timeframes": {}},
            "forecast_scenarios": [],
            "outlook_major_levels": [],
        }
    }
    text = compact_analyst_report_fa(data, live=0)
    assert "بدون پلن اجرایی" in text
    assert "هیچ پلن معتبر Entry / SL / TP صادر نشده" in text
    assert "پلن‌های اجرایی مشروط" not in text
    assert "چشم‌انداز مشروط چهار‌ساعته" in text
    assert "سطوح اصلی:" in text
    assert "Entry: " not in text


def test_telegram_brief_keeps_previous_review_and_one_primary_plan():
    from datetime import datetime, timezone
    from app.services.analyst_brief_report import telegram_analyst_brief
    data = dict(strategy_id="trend_continuation", setup_mode="conditional", potential_direction="LONG",
                entry_price=60511, stop_loss=58666.5, take_profit=63427.7,
                analyst_summary={"previous_review":{"level":{"low":61000,"high":61200},"status_fa":"قیمت به ناحیه نرسید"},
                                 "market_summary_fa":"بازگشت به حمایت محتمل است. " + "شاخص " * 150})
    report = telegram_analyst_brief(data, symbol="BTCUSDT", live=62610, analyzed_at=datetime.now(timezone.utc))
    assert "قیمت به ناحیه نرسید" in report and "بازگشت به حمایت محتمل است." in report
    assert all(label in report for label in ("برگشت","ادامه‌روند","شکست و بازآزمایی"))
    assert report.count("60,511") == 1 and "شاخص" not in report and len(report)<1000
