"""Persian is the display language, not a trading-schema migration."""

from app.services.ai_trader_instructions import ICT_TRADER_INSTRUCTIONS
from app.services.ai_trader_report import build_trader_report_fa, finalize_user_report
from app.services.telegram_bidi import apply_telegram_persian_bidi, strip_telegram_bidi_control_chars
from app.services.ai_telegram_format import build_ai_telegram_header, format_ai_chart_caption
from datetime import datetime, timezone


def test_prompt_requires_persian_prose_but_preserves_schema_codes():
    assert "MANDATORY PERSIAN" in ICT_TRADER_INSTRUCTIONS
    assert "previous_setups_review_fa" in ICT_TRADER_INSTRUCTIONS
    assert "JSON field names" in ICT_TRADER_INSTRUCTIONS
    assert "MSS" in ICT_TRADER_INSTRUCTIONS
    assert "report_fa" in ICT_TRADER_INSTRUCTIONS


def test_farsi_wait_report_keeps_ict_levels_and_no_trade_codes():
    data = {
        "decision": "NO_TRADE",
        "final_ai_decision": "WAIT",
        "setup_mode": "conditional",
        "current_price": 81357.1,
        "market_bias": "Bearish",
        "potential_direction": "SHORT",
        "conditional_entry_zone_low": 81400,
        "conditional_entry_zone_high": 81450,
        "stop_loss": 81650,
        "take_profit": 80900,
        "risk_reward": 2,
        "confirmation_required": "واکنش قیمت به POI و تأیید MSS در 5m لازم است.",
        "conditional_invalidation": "با شکست 81650 سناریو نامعتبر می‌شود.",
        "ict_liquidity": "نقدینگی سمت فروش (SSL) در 81262 جمع‌آوری شد.",
        "ict_structure": "ساختار 4H و 1H نزولی است.",
        "previous_setups_review_fa": "ستاپ قبلی: SHORT در 81467.5\\nنتیجه: پوزیشن آزمایشی باز است.",
    }
    original_decision = data["decision"]
    report = finalize_user_report(data, symbol="BTCUSDT")
    assert report.startswith("تحلیل ICT برای BTCUSDT")
    assert "قیمت:" in report
    assert "81357.1" in report
    assert "روند بازار:\nنزولی" in report
    assert "تصمیم:\nانتظار (WAIT)" in report
    assert "جهت احتمالی:\nشورت (SHORT)" in report
    assert "محدوده ورود:\n81400 - 81450" in report
    assert "تأیید لازم:" in report
    assert "ابطال سناریو:" in report
    assert "دلایل تحلیل ICT:" in report
    assert "نقدینگی سمت فروش (SSL)" in report
    assert "بررسی ستاپ قبلی:" in report
    assert data["decision"] == original_decision
    assert data["stop_loss"] == 81650
    assert "ICT Reason:" not in report


def test_farsi_trade_report_renders_structural_numbers_unchanged():
    data = {
        "decision": "LONG",
        "final_ai_decision": "LONG",
        "current_price": 83201,
        "market_bias": "Bullish",
        "entry_zone_low": 83100,
        "entry_zone_high": 83150,
        "stop_loss": 82900,
        "take_profit": 84100,
        "risk_reward": "1:3",
        "entry_confirmation": "پس از Sweep و MSS تأیید شده است.",
        "ict_fvg": "FVG بین 83100 و 83150 قرار دارد.",
    }
    report = build_trader_report_fa(data)
    assert "تصمیم:\nلانگ (LONG)" in report
    assert "روند بازار:\nصعودی" in report
    assert "ورود:\n83100 - 83150" in report
    assert "حد ضرر (SL):\n82900" in report
    assert "هدف اول (TP1):\n84100" in report
    assert "ریسک به بازده (R:R):\n1:3" in report
    assert "FVG بین 83100 و 83150" in report


def test_telegram_persian_labels_preserve_visible_text_and_ict_terms():
    raw = "دلایل تحلیل ICT:\nدر 5m پس از Sweep، ساختار MSS تأیید شد و FVG معتبر است."
    with_bidi = apply_telegram_persian_bidi(raw)
    visible = strip_telegram_bidi_control_chars(with_bidi)
    assert "دلایل تحلیل ICT:" in visible
    assert "FVG" in with_bidi and "MSS" in with_bidi
    assert "5m" in visible


def test_telegram_header_and_caption_are_farsi():
    header = build_ai_telegram_header(decision="NO_TRADE", validation={"ok": True})
    assert "عدم ورود" in header
    assert "NO TRADE (Paper)" not in header
    caption = format_ai_chart_caption(
        symbol="BTCUSDT", interval="15m", decision="WAIT",
        live_price=81357.1, trade_levels=None,
        analyzed_at=datetime(2026, 10, 17, tzinfo=timezone.utc),
    )
    assert "قیمت لحظه‌ای" in caption
    assert "زمان تحلیل" in caption
    assert "هیچ سفارش واقعی" in caption
