from __future__ import annotations

from datetime import datetime
from typing import Any


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
        lines.append(analyzed_at.astimezone().strftime("Analysis: %Y-%m-%d %H:%M %Z"))
    lines.append("Paper only — no live orders.")
    text = "\n".join(lines)
    return text[:max_len]


def format_ai_telegram_message(
    *,
    decision: str | None,
    report_fa: str,
    validation: dict[str, Any],
    max_body: int = 3200,
) -> str:
    """Plain-text Telegram body (no Markdown) — safe for Persian and arbitrary AI punctuation."""
    decision_u = (decision or "NO_TRADE").upper()
    reasons = validation.get("reasons") or []
    approved = bool(validation.get("ok"))
    trade_like = decision_u in ("LONG", "SHORT")

    if trade_like and not approved:
        header = (
            "تحلیل ICT — INVALID / NOT APPROVED / NO TRADE (Paper)\n"
            f"AI suggestion: {decision_u} — rejected by backend validator.\n"
            f"Reasons: {', '.join(reasons) if reasons else 'validation_failed'}\n"
            "Levels below are NOT actionable. Paper only — do not trade.\n\n"
        )
    elif decision_u == "NO_TRADE" or not trade_like:
        header = "تحلیل ICT — NO TRADE (Paper)\n\n"
    else:
        header = f"تحلیل ICT — VALID / validator APPROVED {decision_u} (Paper)\n\n"

    body = (report_fa or "")[:max_body]
    return header + body
