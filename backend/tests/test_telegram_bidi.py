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
    visible = strip_telegram_bidi_control_chars(out)
    assert visible.startswith("Bias: پس از بررسی")
    assert visible.endswith("نزولی است.")


def test_bidi_persian_with_ict_terms_unchanged_visible():
    raw = (
        "ICT Reason:\n"
        "قیمت به FVG و MSS در تایم 15m واکنش نشان داد؛ BOS هنوز تأیید نشده و OB قبلی معتبر است."
    )
    out = apply_telegram_persian_bidi(raw)
    assert "FVG" in out and "MSS" in out and "BOS" in out and "OB" in out
    visible = strip_telegram_bidi_control_chars(out)
    assert visible.startswith("ICT Reason: قیمت")


def test_bidi_merges_english_label_value_pairs():
    raw = "Decision:\nLONG\nEntry:\n98500.5"
    out = apply_telegram_persian_bidi(raw)
    visible = strip_telegram_bidi_control_chars(out)
    assert visible == "Decision: LONG\nEntry: 98500.5"


def test_telegram_format_path_preserves_visible_report_content():
    from app.services.ai_telegram_format import build_ai_telegram_header

    report = "Bias:\nپس از بررسی ساختار 4H و 15m، روند کلی نزولی است.\nSL:\n91200"
    msg = format_ai_telegram_message(
        decision="SHORT",
        report_fa=report,
        validation={"ok": True},
    )
    assert strip_telegram_bidi_control_chars(msg).endswith(report.split("\n")[-1])
    assert "Bias:" in strip_telegram_bidi_control_chars(msg)
    assert strip_telegram_bidi_control_chars(msg).count("91200") == 1


def test_split_long_report_strips_to_original_body():
    body = "Bias:\n" + ("ف" * 4000)
    parts = split_ai_telegram_report_messages(
        decision="NO_TRADE",
        report_fa=body,
        validation={"ok": True},
    )
    joined = strip_telegram_bidi_control_chars("".join(parts))
    idx = joined.index("Bias:")
    assert joined[idx:].replace("Bias: ", "Bias:\n") == body
