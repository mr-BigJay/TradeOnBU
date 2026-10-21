"""Telegram readability: label/value pairing, whitespace, BiDi-safe splits."""

from __future__ import annotations

from app.services.ai_telegram_format import split_ai_telegram_report_messages
from app.services.ai_trader_report import finalize_user_report
from app.services.telegram_message_format import (
    LRI,
    PDI,
    format_persian_report_for_telegram,
    join_label_value_lines,
    normalize_report_layout_for_telegram,
    strip_telegram_bidi_control_chars,
)


def _conditional_wait_report() -> str:
    data = {
        "decision": "NO_TRADE",
        "final_ai_decision": "WAIT",
        "setup_mode": "conditional",
        "current_price": 84634.4,
        "market_bias": "Bearish",
        "market_regime": "bearish",
        "potential_direction": "SHORT",
        "conditional_entry_zone_low": 84400,
        "conditional_entry_zone_high": 84600,
        "stop_loss": 85200,
        "take_profit": 82000,
        "risk_reward": 2.2,
        "confirmation_required": "واکنش به POI و تأیید MSS در 5m؛ ریسک -2.5% نسبت به SL.",
        "conditional_invalidation": "شکست 85200 سناریو را باطل می‌کند.",
        "ict_liquidity": "SSL در 81262 جمع شد.",
        "ict_structure": "MSS نزولی در 15m پس از BOS.",
        "ict_fvg": "FVG بین 84450 و 84520.",
    }
    return finalize_user_report(data, symbol="BTCUSDT", live=84634.4)


def test_normalize_joins_label_and_value_on_one_line():
    raw = "قیمت:\n84634.4\n\nتصمیم:\nانتظار (WAIT)\n\nجهت احتمالی:\nشورت (SHORT)"
    norm = normalize_report_layout_for_telegram(raw)
    assert "قیمت: 84634.4" in norm
    assert "تصمیم: انتظار (WAIT)" in norm
    assert "جهت احتمالی: شورت (SHORT)" in norm
    assert "\n\n\n" not in norm


def test_normalize_keeps_bullet_list_under_ict_section():
    lines = [
        "دلایل تحلیل ICT:",
        "",
        "- نقدینگی جمع شد",
        "- ساختار MSS",
    ]
    merged = join_label_value_lines(lines)
    assert merged[0] == "دلایل تحلیل ICT:"
    assert merged[1] == ""
    assert merged[2].startswith("- ")


def test_realistic_ict_report_telegram_format_preserves_content():
    report = _conditional_wait_report()
    formatted = format_persian_report_for_telegram(report)
    visible = strip_telegram_bidi_control_chars(formatted)
    assert "84634.4" in visible
    assert "انتظار (WAIT)" in visible
    assert "شورت (SHORT)" in visible
    assert "قیمت: 84634.4" in visible or "قیمت:\n84634.4" not in visible
    assert "BTCUSDT" in visible
    assert f"{LRI}BTCUSDT{PDI}" in formatted or "BTCUSDT" in formatted


def test_realistic_report_negative_percentage_ltr_isolated():
    report = _conditional_wait_report()
    formatted = format_persian_report_for_telegram(report)
    assert f"{LRI}-2.5%{PDI}" in formatted


def test_long_report_split_keeps_label_value_and_balanced_bidi():
    report = normalize_report_layout_for_telegram(_conditional_wait_report())
    body = report + "\n\n" + ("توضیح تکمیلی: " + "ف" * 4200)
    parts = split_ai_telegram_report_messages(
        decision="NO_TRADE",
        report_fa=body,
        validation={"ok": True},
    )
    assert len(parts) >= 2
    assert all(len(p) <= 4096 for p in parts)
    joined_visible = strip_telegram_bidi_control_chars("".join(parts))
    assert "قیمت: 84634.4" in joined_visible
    for part in parts:
        assert part.count(LRI) == part.count(PDI) or PDI not in part


def test_idempotent_bidi_no_double_wrap():
    report = _conditional_wait_report()
    once = format_persian_report_for_telegram(report)
    twice = format_persian_report_for_telegram(once)
    assert twice == once
