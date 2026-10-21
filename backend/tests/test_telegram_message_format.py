"""Regression: Persian RTL, LTR islands, escaping, chunking for Telegram AI reports."""

from __future__ import annotations

from app.services.ai_telegram_format import split_ai_telegram_report_messages
from app.services.telegram_message_format import (
    LRI,
    PDI,
    TELEGRAM_MESSAGE_MAX_LENGTH,
    chunk_telegram_text,
    escape_telegram_html,
    escape_telegram_markdown_legacy,
    format_persian_report_for_telegram,
    isolate_ltr_tokens_in_persian,
    normalize_report_layout_for_telegram,
    prepare_telegram_outbound_text,
    split_header_and_body_chunks,
    strip_telegram_bidi_control_chars,
)


def test_ltr_islands_symbol_direction_and_signed_price():
    raw = "جهت BTCUSDT روی LONG با ورود 98,500.5 و SL -91200.25 است."
    out = isolate_ltr_tokens_in_persian(raw)
    assert f"{LRI}BTCUSDT{PDI}" in out
    assert f"{LRI}LONG{PDI}" in out
    assert f"{LRI}98,500.5{PDI}" in out
    assert f"{LRI}-91200.25{PDI}" in out
    assert strip_telegram_bidi_control_chars(out) == raw


def test_ltr_islands_negative_percentage_and_timestamp():
    raw = "ریسک -2.5% در ساعت 14:30 و تاریخ 2026-10-21 16:00 بررسی شد."
    out = isolate_ltr_tokens_in_persian(raw)
    assert f"{LRI}-2.5%{PDI}" in out
    assert f"{LRI}14:30{PDI}" in out
    assert f"{LRI}2026-10-21 16:00{PDI}" in out
    assert strip_telegram_bidi_control_chars(out) == raw


def test_mixed_persian_english_line_unchanged_visible():
    raw = "قیمت لحظه‌ای: 81,357.10 USDT — تایم‌فریم 15m"
    out = format_persian_report_for_telegram(raw)
    assert strip_telegram_bidi_control_chars(out) == raw
    assert f"{LRI}81,357.10{PDI}" in out
    assert f"{LRI}15m{PDI}" in out


def test_english_only_line_merges_without_bidi_controls():
    raw = "Decision:\nLONG\nEntry:\n98500.5"
    visible = strip_telegram_bidi_control_chars(format_persian_report_for_telegram(raw))
    assert visible == "Decision: LONG\nEntry: 98500.5"
    assert LRI not in format_persian_report_for_telegram(raw)


def test_html_escape_user_content():
    assert escape_telegram_html("<tag>&") == "&lt;tag&gt;&amp;"


def test_markdown_legacy_escape_preserves_visible():
    raw = "_italic_ *bold* `code` [link]"
    escaped = escape_telegram_markdown_legacy(raw)
    assert escaped == "\\_italic\\_ \\*bold\\* \\`code\\` \\[link]"


def test_prepare_plain_applies_bidi_not_html_escape():
    raw = "تحلیل <b>مهم</b> برای BTCUSDT"
    out = prepare_telegram_outbound_text(raw, parse_mode=None)
    assert "<b>" in out
    assert f"{LRI}BTCUSDT{PDI}" in out


def test_prepare_html_escapes_without_bidi():
    raw = "تحلیل <script> & BTCUSDT"
    out = prepare_telegram_outbound_text(raw, parse_mode="HTML")
    assert "&lt;script&gt;" in out
    assert LRI not in out


def test_chunk_prefers_newlines_and_respects_limit():
    line = "x" * 100
    text = "\n".join([line] * 50)
    parts = chunk_telegram_text(text, limit=500)
    assert len(parts) > 1
    assert all(len(p) <= 500 for p in parts)
    assert strip_telegram_bidi_control_chars("".join(parts)) == text


def test_split_header_body_long_persian_report():
    body = "Bias:\n" + ("ف" * 4200)
    parts = split_header_and_body_chunks(header="HEADER\n", body=body, limit=4096)
    assert len(parts) >= 2
    assert all(len(p) <= TELEGRAM_MESSAGE_MAX_LENGTH for p in parts)
    joined = "".join(parts)
    assert joined.startswith("HEADER")
    assert strip_telegram_bidi_control_chars(joined).endswith("ف")


def test_split_ai_telegram_integration_strips_to_original_body():
    report = "Bias:\n" + ("ن" * 4500)
    parts = split_ai_telegram_report_messages(
        decision="NO_TRADE",
        report_fa=report,
        validation={"ok": True},
    )
    body_only = strip_telegram_bidi_control_chars("".join(parts))
    assert report in body_only or body_only.endswith("ن")


def test_label_then_persian_value_merged_on_one_line():
    raw = "Bias:\nپس از بررسی ساختار بازار، روند کلی نزولی است."
    out = format_persian_report_for_telegram(raw)
    visible = strip_telegram_bidi_control_chars(out)
    assert visible.startswith("Bias: پس از بررسی")
    assert "نزولی است." in visible


def test_normalize_collapses_excessive_blank_lines():
    raw = "قیمت:\n100\n\n\n\nتصمیم:\nWAIT"
    norm = normalize_report_layout_for_telegram(raw)
    assert "\n\n\n" not in norm
    assert "قیمت: 100" in norm
