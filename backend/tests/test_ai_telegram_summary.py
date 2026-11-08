from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.services.ai_ict_analysis import _RunDraft, _deliver_ai_telegram
from app.services.ai_telegram_summary import (
    build_ai_telegram_caption,
    build_ai_telegram_summary,
)
from app.services.telegram_message_format import strip_telegram_bidi_control_chars

NOW = datetime(2026, 11, 8, 8, tzinfo=timezone.utc)


def sample():
    return dict(final_ai_decision="WAIT", setup_mode="conditional", potential_direction="LONG",
                strategy_id="ict_reversal", market_regime="bearish", trade_against_dominant_trend=True,
                entry_price=77247.85, entry_zone_low=77217.9, entry_zone_high=77277.8,
                stop_loss=76750, take_profit=79765.3,
                ict_structure="ساختار نزولی است؛ MSS هنوز تأیید نشده.",
                confirmation_required="جاروب، شکست ساختار و بازآزمایی ناحیه لازم است.",
                conditional_invalidation="بسته‌شدن زیر 76750")


def outlook_sample():
    return {
        "final_ai_decision": "NO_TRADE",
        "setup_mode": "flat",
        "decision": "NO_TRADE",
        "market_regime": "unclear",
        "analyst_summary": {
            "accepted": 0,
            "proposed": 0,
            "market_summary_fa": "BTCUSDT اکنա روند نزولی دارد و RSI14 برابر 40.2 است.",
            "comparison_fa": "در وضعیت فعلی، واکنش به ناحیه باید با بسته‌شدن کندل تأیید شود.",
            "execution_regime": {"timeframes": {
                "4h": {"trend": "bearish"},
                "1h": {"trend": "range"},
            }},
            "forecast_scenarios": [
                {
                    "scenario_type": "resistance_rejection", "direction": "SHORT",
                    "level": {"kind": "resistance", "timeframe": "15m",
                              "low": 75889.4, "high": 75908.6,
                              "status": "retested", "touch_count": 1},
                    "target": {"kind": "support", "timeframe": "15m",
                               "low": 75820.0, "high": 75825.0},
                    "reason_fa": "75889.4 fresh 5m դիմադրության վերևում փակված մոմի retest",
                    "condition_fa": "رسیدن قیمت به مقاومت، واکنش و تأیید برگشت در کندل بستهٔ 5m.",
                    "invalidation_fa": "تثبیت بالای سقف مقاومت، سناریو را بی‌اعتبار می‌کند.",
                },
                {
                    "scenario_type": "support_bounce", "direction": "LONG",
                    "level": {"kind": "support", "timeframe": "15m",
                              "low": 75615.0, "high": 75626.0,
                              "status": "fresh", "touch_count": 0},
                    "target": {"kind": "resistance", "timeframe": "1h",
                               "low": 75890.0, "high": 75910.0},
                    "reason_fa": "حمایت تازه است و هنوز برخورد مجزایی پس از تأیید نداشته است.",
                    "condition_fa": "واکنش صعودی و تأیید کندل 5m لازم است.",
                    "invalidation_fa": "تثبیت زیر کف حمایت، سناریو را باطل می‌کند.",
                },
                {
                    "scenario_type": "support_break", "direction": "SHORT",
                    "level": {"kind": "support", "timeframe": "1h",
                              "low": 75200.0, "high": 75220.0,
                              "status": "retested", "touch_count": 3},
                    "target": {"kind": "support", "timeframe": "4h",
                               "low": 74800.0, "high": 74820.0},
                    "reason_fa": "سه برخورد مجزا ثبت شده؛ شکست فقط پس از بسته‌شدن و بازآزمایی معتبر است.",
                    "condition_fa": "بسته‌شدن 5m زیر حمایت و بازآزمایی از پایین لازم است.",
                    "invalidation_fa": "بازگشت و تثبیت بالای حمایت، سناریو را بی‌اعتبار می‌کند.",
                },
                {
                    "scenario_type": "resistance_break", "direction": "LONG",
                    "level": {"kind": "resistance", "timeframe": "15m",
                              "low": 75940.0, "high": 75960.0,
                              "status": "fresh", "touch_count": 0},
                    "target": {"kind": "resistance", "timeframe": "1h",
                               "low": 76000.0, "high": 76020.0},
                    "reason_fa": "مقاومت دست‌نخورده است؛ شکست بدون بازآزمایی معتبر نیست.",
                    "condition_fa": "بسته‌شدن 5m بالای مقاومت و بازآزمایی از بالا لازم است.",
                    "invalidation_fa": "بازگشت زیر مقاومت، سناریو را بی‌اعتبار می‌کند.",
                },
            ],
        },
        "_artifacts": {
            "strategy_features": {"timeframes": {
                "4h": {"available": True, "ema9_ema20_relation": "below",
                       "rsi14": 40.2, "adx14": 33.5, "atr14": 764.3,
                       "relative_volume20": 0.10},
                "1h": {"available": True, "ema9_ema20_relation": "below",
                       "rsi14": 50.8, "adx14": 28.7, "atr14": 184.5,
                       "relative_volume20": 0.42},
            }},
        },
    }


def test_summary_preserves_levels_wait_and_does_not_mutate_analysis():
    s = sample()
    original = deepcopy(s)
    text = build_ai_telegram_summary(symbol="BTCUSDT", live=77387.5, decision="NO_TRADE",
                                    structured=s, validation={"ok": True}, report_fa="full report", analyzed_at=NOW)
    plain = strip_telegram_bidi_control_chars(text)
    for value in ("77,247.85", "77,217.9", "77,277.8", "76,750", "79,765.3", "1:2.5",
                  "انتظار", "خلاف روند", "شرط ورود", "ابطال"):
        assert value in plain
    assert "\u20661:2.5\u2069" in text
    assert s == original
    assert len(text.encode("utf-16-le")) // 2 <= 4096


def test_zero_accepted_plans_still_delivers_four_hour_outlook_and_regimes():
    s = outlook_sample()
    original = deepcopy(s)
    text = build_ai_telegram_summary(
        symbol="BTCUSDT", live=75899.2, decision="NO_TRADE", structured=s,
        validation={"ok": True}, report_fa="گزارش کامل", analyzed_at=NOW,
    )
    plain = strip_telegram_bidi_control_chars(text)
    for value in (
        "بدون پلن اجرایی پذیرفته‌شده",
        "عدم ورود؛ 4 سناریوی مشروط برای رصد",
        "روند ساختاری: 4H نزولی | 1H رنج",
        "چشم‌انداز چهار‌ساعته",
        "واکنش احتمالی به مقاومت",
        "برگشت احتمالی از حمایت",
        "شکست و بازآزمایی حمایت",
        "شکست و بازآزمایی مقاومت",
        "دست‌نخورده",
        "تعداد برخوردهای مجزا: 3",
        "مقصد ساختاری مشروط",
        "RSI14=40.2",
        "ADX14=33.5",
    ):
        assert value in plain
    assert "بدون سناریوی فعال" not in plain
    assert "Ա" not in plain
    assert s == original
    assert len(text.encode("utf-16-le")) // 2 <= 4096

    caption = build_ai_telegram_caption(
        symbol="BTCUSDT", live=75899.2, decision="NO_TRADE",
        structured=s, validation={"ok": True}, analyzed_at=NOW,
    )
    assert len(caption.encode("utf-16-le")) // 2 <= 1024
    assert "پیام متنی بعدی" in strip_telegram_bidi_control_chars(caption)


@pytest.mark.parametrize("decision", ["LONG", "SHORT", "NO_TRADE", "WAIT"])
def test_long_unicode_summary_and_caption_fit_telegram_limits(decision):
    s = sample()
    s.update(final_ai_decision=decision, setup_mode="immediate")
    for key in ("ict_structure", "confirmation_required", "conditional_invalidation"):
        s[key] = "متن طولانی 🚀 77387.5 BTCUSDT " * 1000
    text = build_ai_telegram_summary(symbol="BTCUSDT", live=77387.5, decision=decision,
                                    structured=s, validation={"ok": False, "reasons": ["رد"]},
                                    report_fa="full", analyzed_at=NOW)
    caption = build_ai_telegram_caption(symbol="BTCUSDT", live=77387.5, decision=decision,
                                      structured=s, validation={"ok": False}, analyzed_at=NOW)
    assert len(text.encode("utf-16-le")) // 2 <= 4096
    assert len(caption.encode("utf-16-le")) // 2 <= 1024
    assert text.count("\u2066") == text.count("\u2069")
    if decision in ("LONG", "SHORT"):
        assert "ورود مجاز نیست" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("photo_result,chart_exists", [(42, True), (None, True), (None, False)])
async def test_delivery_sends_full_text_with_or_without_chart_and_preserves_report(
    tmp_path, photo_result, chart_exists
):
    chart = tmp_path / "chart.png"
    if chart_exists:
        chart.write_bytes(b"fake photo")
    s = outlook_sample()
    report = "گزارش کامل " * 1000
    draft = _RunDraft(decision="NO_TRADE", structured=deepcopy(s),
                      validation={"ok": True}, report_fa=report)
    with patch("app.services.ai_ict_analysis.TelegramNotifier") as cls:
        notifier = cls.return_value
        notifier.enabled = True
        notifier.send_photo_file = AsyncMock(return_value=photo_result)
        notifier.send_ai_report = AsyncMock(return_value=43)
        await _deliver_ai_telegram(draft=draft, symbol="BTCUSDT", live=75899.2,
                                   chart_path=chart if chart_exists else None, analyzed_at=NOW)
        assert notifier.send_photo_file.await_count == int(chart_exists)
        notifier.send_ai_report.assert_awaited_once()
        full_text = notifier.send_ai_report.call_args.args[0]
        assert "چشم‌انداز چهار‌ساعته" in strip_telegram_bidi_control_chars(full_text)
        assert len(full_text.encode("utf-16-le")) // 2 <= 4096
        if photo_result is not None and chart_exists:
            caption = notifier.send_photo_file.call_args.args[1]
            assert len(caption.encode("utf-16-le")) // 2 <= 1024
    assert draft.telegram_sent
    assert draft.telegram_photo_sent is (photo_result is not None and chart_exists)
    assert draft.report_fa == report
    for key in s:
        assert draft.structured[key] == s[key]
