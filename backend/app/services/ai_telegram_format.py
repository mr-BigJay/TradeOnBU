from __future__ import annotations

from datetime import datetime
from typing import Any

from app.domain.tehran_time import format_tehran
from app.services.telegram_bidi import apply_telegram_persian_bidi

TELEGRAM_PLAIN_TEXT_LIMIT = 4096


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
        f"Decision: {decision_u}",
    ]
    if live_price and live_price > 0:
        lines.append(f"Live: {live_price:,.2f}")
    if trade_levels and decision_u in ("LONG", "SHORT"):
        for key, label in (("entry", "Entry"), ("stop_loss", "SL"), ("take_profit", "TP")):
            val = trade_levels.get(key)
            if val is not None and val > 0:
                lines.append(f"{label}: {val:,.2f}")
    if analyzed_at:
        lines.append(f"Analysis (Tehran): {format_tehran(analyzed_at)}")
    lines.append("Paper only — no live orders.")
    text = "\n".join(lines)
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
            "تحلیل ICT — INVALID / NOT APPROVED / NO TRADE (Paper)\n"
            f"AI suggestion: {decision_u} — rejected by backend validator.\n"
            f"Reasons: {', '.join(reasons) if reasons else 'validation_failed'}\n"
            "Levels below are NOT actionable. Paper only — do not trade.\n\n"
        )
    if decision_u == "NO_TRADE" or not trade_like:
        return "تحلیل ICT — NO TRADE (Paper)\n\n"
    return f"تحلیل ICT — VALID / validator APPROVED {decision_u} (Paper)\n\n"


def split_ai_telegram_report_messages(
    *,
    decision: str | None,
    report_fa: str,
    validation: dict[str, Any],
    limit: int = TELEGRAM_PLAIN_TEXT_LIMIT,
) -> list[str]:
    """Split full Persian report into Telegram-safe plain-text messages (header on first part only)."""
    header = build_ai_telegram_header(decision=decision, validation=validation)
    body = apply_telegram_persian_bidi(report_fa or "")
    if not body:
        return [header.rstrip()] if header.strip() else []

    if len(header) + len(body) <= limit:
        return [header + body]

    messages: list[str] = []
    first_body_budget = limit - len(header)
    if first_body_budget <= 0:
        messages.append(header[:limit])
        cursor = 0
    else:
        messages.append(header + body[:first_body_budget])
        cursor = first_body_budget

    while cursor < len(body):
        chunk = body[cursor : cursor + limit]
        messages.append(chunk)
        cursor += limit
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
