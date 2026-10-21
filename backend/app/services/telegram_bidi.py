"""Backward-compatible re-exports; prefer telegram_message_format."""

from __future__ import annotations

from app.services.telegram_message_format import (
    LRI,
    PDI,
    RLI,
    format_persian_report_for_telegram,
    strip_telegram_bidi_control_chars,
)

apply_telegram_persian_bidi = format_persian_report_for_telegram

__all__ = [
    "LRI",
    "PDI",
    "RLI",
    "apply_telegram_persian_bidi",
    "strip_telegram_bidi_control_chars",
]
