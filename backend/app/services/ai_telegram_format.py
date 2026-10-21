from __future__ import annotations

from datetime import datetime
from typing import Any

from app.domain.tehran_time import format_tehran
from app.services.telegram_message_format import (
    TELEGRAM_MESSAGE_MAX_LENGTH,
    format_persian_report_for_telegram,
    split_header_and_body_chunks,
)

TELEGRAM_PLAIN_TEXT_LIMIT = TELEGRAM_MESSAGE_MAX_LENGTH


def format_ai_chart_caption(
    *,
    symbol: str,
    interval: str,
    decision: str | None,
    live_price: float | None,
    trade_levels: dict[str, float] | None,
    analyzed_at: datetime | None,
    max_len: int = 1024,
) -> str:
    decision_u = (decision or "NO_TRADE").upper()
    lines = [
        f"TradeOnBU · {symbol} · {interval}",
        f"تصمیم: {decision_u}",
    ]
    if live_price and live_price > 0:
        lines.append(f"قیمت لحظه‌ای: {live_price:,.2f}")
    if trade_levels and decision_u in ("LONG", "SHORT"):
        for key, label in (("entry", "ورود"), ("stop_loss", "حد ضرر (SL)"), ("take_profit", "حد سود (TP)")):
            val = trade_levels.get(key)
            if val is not None and val > 0:
                lines.append(f"{label}: {val:,.2f}")
    if analyzed_at:
        lines.append(f"زمان تحلیل (تهران): {format_tehran(analyzed_at)}")
    lines.append("فقط معاملات آزمایشی؛ هیچ سفارش واقعی ثبت نمی‌شود.")
    text = format_persian_report_for_telegram("\n".join(lines))
    return text[:max_len]


def build_ai_telegram_header(
    *,
    decision: str | None,
    validation: dict[str, Any],
) -> str:
    decision_u = (decision or "NO_TRADE").upper()
    reasons = validation.get("reasons") or []
    approved = bool(validation.get("ok"))
    trade_like = decision_u in ("LONG", "SHORT")

    if trade_like and not approved:
        return (
            "تحلیل ICT — سیگنال تأیید نشده (معامله آزمایشی)\n"
            f"پیشنهاد مدل: {decision_u}؛ اعتبارسنجی سامانه آن را رد کرده است.\n"
            f"دلایل: {', '.join(reasons) if reasons else 'عدم تأیید اعتبارسنجی'}\n"
            "سطوح زیر قابل اقدام نیستند. هیچ سفارش واقعی ثبت نمی‌شود.\n\n"
        )
    if decision_u == "NO_TRADE" or not trade_like:
        return "تحلیل ICT — عدم ورود (معامله آزمایشی)\n\n"
    return f"تحلیل ICT — تأیید شده: {decision_u} (معامله آزمایشی)\n\n"


def split_ai_telegram_report_messages(
    *,
    decision: str | None,
    report_fa: str,
    validation: dict[str, Any],
    limit: int = TELEGRAM_PLAIN_TEXT_LIMIT,
) -> list[str]:
    """Split full Persian report into Telegram-safe plain-text messages (header on first part only)."""
    header = build_ai_telegram_header(decision=decision, validation=validation)
    raw_body = report_fa or ""
    if not raw_body.strip():
        return split_header_and_body_chunks(header=header, body="", limit=limit)
    from app.services.telegram_message_format import chunk_telegram_text

    raw_parts = split_header_and_body_chunks(header=header, body=raw_body, limit=limit)
    messages: list[str] = []
    for part in raw_parts:
        formatted = format_persian_report_for_telegram(part)
        messages.extend(chunk_telegram_text(formatted, limit=limit))
    return messages


def format_ai_telegram_message(
    *,
    decision: str | None,
    report_fa: str,
    validation: dict[str, Any],
    max_body: int | None = None,
) -> str:
    """Return the first Telegram part (full content when it fits in one message)."""
    _ = max_body  # legacy arg; splitting handled by split_ai_telegram_report_messages
    parts = split_ai_telegram_report_messages(
        decision=decision,
        report_fa=report_fa,
        validation=validation,
    )
    return parts[0] if parts else ""
