"""Assemble user-facing ICT trader report from structured AI JSON (no internal system leakage)."""

from __future__ import annotations

import re
from typing import Any

FORBIDDEN_REPORT_SUBSTRINGS = (
    "lifecycle_phase",
    "first_touch_ms",
    "objective_notes",
    "memory.open",
    "memory_rules",
    "gaps_remaining",
    "data_complete",
    "tracking_warning",
    "expiry_boundary",
    "outcome_tracking",
    "run_id",
    "setup_id",
    "OHLC validation",
    "uncertainty_notes",
    "paper_fill_recorded",
    "validity_status",
)

MAX_REPORT_WORDS = 800


def _s(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return str(v)
    return str(v).strip()


def _entry_zone(data: dict[str, Any]) -> str:
    lo = data.get("entry_zone_low")
    hi = data.get("entry_zone_high")
    ep = data.get("entry_price")
    if lo is not None and hi is not None:
        return f"{_s(lo)}-{_s(hi)}"
    if ep is not None:
        return _s(ep)
    return ""


def sanitize_trader_text(text: str) -> str:
    if not text:
        return ""
    out = text
    for bad in FORBIDDEN_REPORT_SUBSTRINGS:
        out = re.sub(re.escape(bad), "", out, flags=re.IGNORECASE)
    out = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "", out, flags=re.I)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _truncate_words(text: str, max_words: int = MAX_REPORT_WORDS) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]) + "…"


def build_trader_report_fa(data: dict[str, Any], *, symbol: str = "BTCUSDT") -> str:
    """Decision-first report matching manual ChatGPT ICT layout."""
    decision = _s(data.get("final_ai_decision") or data.get("decision") or "NO_TRADE").upper()
    trade_decision = _s(data.get("decision") or decision).upper()
    price = data.get("current_price") or data.get("reference_price")
    bias = data.get("market_bias") or data.get("bias") or data.get("structure_summary")

    lines: list[str] = [
        f"{symbol} ICT Analysis",
        "",
        f"Current Price:",
        _s(price) or "—",
        "",
        f"Market Bias:",
        sanitize_trader_text(_s(bias)) or "—",
        "",
        f"Decision:",
        decision,
        "",
    ]

    if trade_decision in ("LONG", "SHORT"):
        lines.extend(
            [
                "Setup:",
                trade_decision,
                "",
                "Entry:",
                _entry_zone(data) or "—",
                "",
                "SL:",
                _s(data.get("stop_loss")) or "—",
                "",
                "TP1:",
                _s(data.get("take_profit")) or "—",
                "",
            ]
        )
        tp2 = data.get("take_profit_2")
        if tp2 is not None:
            lines.extend(["TP2:", _s(tp2), ""])
        lines.extend(
            [
                "RR:",
                _s(data.get("risk_reward")) or "—",
                "",
            ]
        )

    bullets = [
        ("Liquidity", data.get("ict_liquidity") or data.get("liquidity")),
        ("Structure", data.get("ict_structure") or data.get("mss_bos")),
        ("Displacement", data.get("ict_displacement") or data.get("displacement")),
        ("FVG", data.get("ict_fvg") or data.get("fvg")),
        ("Order Block", data.get("ict_order_block") or data.get("order_blocks")),
    ]
    lines.extend(["ICT Analysis:", ""])
    for _label, val in bullets:
        text = sanitize_trader_text(_s(val))
        if text:
            lines.append(f"- {text}")
    lines.append("")

    if trade_decision in ("LONG", "SHORT"):
        lines.extend(
            [
                "Entry Plan:",
                "",
                "Entry:",
                _entry_zone(data) or "—",
                "",
                "Trigger:",
                sanitize_trader_text(_s(data.get("entry_trigger") or data.get("entry_confirmation"))) or "—",
                "",
                "Invalidation:",
                sanitize_trader_text(_s(data.get("invalidation_condition") or data.get("invalidation"))) or "—",
                "",
            ]
        )
    elif decision == "NO_TRADE" or trade_decision == "NO_TRADE":
        reasons = data.get("no_trade_reasons") or []
        if isinstance(reasons, list) and reasons:
            lines.extend(["Reason:", ""])
            for r in reasons[:5]:
                lines.append(f"- {sanitize_trader_text(_s(r))}")
            lines.append("")
        waiting = data.get("waiting_for") or []
        if isinstance(waiting, list) and waiting:
            lines.extend(["Waiting for:", ""])
            for w in waiting[:5]:
                lines.append(f"- {sanitize_trader_text(_s(w))}")
            lines.append("")

    prev = sanitize_trader_text(_s(data.get("previous_setups_review_fa")))
    if prev:
        lines.extend(["Previous Setup Review:", "", prev, ""])

    report = "\n".join(lines).strip()
    return _truncate_words(sanitize_trader_text(report))
