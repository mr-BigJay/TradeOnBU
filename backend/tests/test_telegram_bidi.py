from __future__ import annotations

from app.services.ai_telegram_format import format_ai_telegram_message, split_ai_telegram_report_messages
from app.services.telegram_bidi import (
    PDI,
    RLI,
    apply_telegram_persian_bidi,
    strip_telegram_bidi_control_chars,
)


def test_bidi_english_label_then_persian_paragraph():
    raw = "Bias:\nپس از بررسی ساختار بازار، روند کلی نزولی است."
    out = apply_telegram_persian_bidi(raw)
    assert out.startswith("Bias:\n")
    assert f"{RLI}پس از بررسی" in out
    assert out.endswith(f"نزولی است.{PDI}")
    assert strip_telegram_bidi_control_chars(out) == raw


def test_bidi_persian_with_ict_terms_unchanged_visible():
    raw = (
        "ICT Reason:\n"
        "قیمت به FVG و MSS در تایم 15m واکنش نشان داد؛ BOS هنوز تأیید نشده و OB قبلی معتبر است."
    )
    out = apply_telegram_persian_bidi(raw)
    assert "FVG" in out and "MSS" in out and "BOS" in out and "OB" in out
    assert strip_telegram_bidi_control_chars(out) == raw


def test_bidi_does_not_mutate_english_value_lines():
    raw = "Decision:\nLONG\nEntry:\n98500.5"
    out = apply_telegram_persian_bidi(raw)
    assert out == raw


def test_telegram_format_path_preserves_visible_report_content():
    from app.services.ai_telegram_format import build_ai_telegram_header

    report = "Bias:\nپس از بررسی ساختار 4H و 15m، روند کلی نزولی است.\nSL:\n91200"
    msg = format_ai_telegram_message(
        decision="SHORT",
        report_fa=report,
        validation={"ok": True},
    )
    header = build_ai_telegram_header(decision="SHORT", validation={"ok": True})
    body_in_msg = msg[len(header) :]
    assert strip_telegram_bidi_control_chars(body_in_msg) == report


def test_split_long_report_strips_to_original_body():
    body = "Bias:\n" + ("ف" * 4000)
    parts = split_ai_telegram_report_messages(
        decision="NO_TRADE",
        report_fa=body,
        validation={"ok": True},
    )
    joined = "".join(parts)
    assert RLI in joined
    # Visible body chars only (skip dynamic header prefix).
    idx = joined.index("Bias:")
    assert strip_telegram_bidi_control_chars(joined[idx:]) == body
