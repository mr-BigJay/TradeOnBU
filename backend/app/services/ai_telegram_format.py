from __future__ import annotations

from typing import Any


def format_ai_telegram_message(
    *,
    decision: str | None,
    report_fa: str,
    validation: dict[str, Any],
    max_body: int = 3200,
) -> str:
    decision_u = (decision or "NO_TRADE").upper()
    reasons = validation.get("reasons") or []
    approved = bool(validation.get("ok"))
    trade_like = decision_u in ("LONG", "SHORT")

    if trade_like and not approved:
        header = (
            "*تحلیل ICT — INVALID / NOT APPROVED / NO TRADE (Paper)*\n"
            f"AI suggestion: `{decision_u}` — **rejected by backend validator**.\n"
            f"Reasons: `{', '.join(reasons) if reasons else 'validation_failed'}`\n"
            "_Levels below are NOT actionable. Paper only — do not trade._\n\n"
        )
    elif decision_u == "NO_TRADE" or not trade_like:
        header = "*تحلیل ICT — NO TRADE (Paper)*\n\n"
    else:
        header = f"*تحلیل ICT — validator APPROVED `{decision_u}` (Paper)*\n\n"

    body = (report_fa or "")[:max_body]
    return header + body
