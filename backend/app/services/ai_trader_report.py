"""User-facing ICT trader report — server-only assembly, zero internal leakage."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

# Substrings / patterns that must never appear in user report_fa
FORBIDDEN_SUBSTRINGS = (
    "memory.",
    "memory_rules",
    "open_setups",
    "recent_closed_setups",
    "lifecycle_phase",
    "first_touch_ms",
    "objective_notes",
    "gaps_remaining",
    "data_complete",
    "data quality",
    "data_quality",
    "tracking_warning",
    "expiry_boundary",
    "outcome_tracking",
    "paper_fill_recorded",
    "validity_status",
    "uncertainty_notes",
    "uncertainty",
    "OHLC validation",
    "ohlc validation",
    "candle inconsistency",
    "candle anomaly",
    "previous run",
    "run_id",
    "setup_id",
    "database",
    "backend error",
    "validation failed",
    "token_budget",
    "reconciling_expired",
)

FORBIDDEN_REGEX = (
    re.compile(r"\bmemory\b", re.I),
    re.compile(r"```json", re.I),
    re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I),
)

MAX_REPORT_WORDS = 700

TRADER_STRING_KEYS = (
    "market_bias",
    "bias",
    "structure_summary",
    "ict_liquidity",
    "ict_structure",
    "ict_displacement",
    "ict_fvg",
    "ict_order_block",
    "liquidity",
    "mss_bos",
    "displacement",
    "fvg",
    "order_blocks",
    "entry_trigger",
    "entry_confirmation",
    "invalidation_condition",
    "invalidation",
    "trade_reason",
    "previous_setups_review_fa",
    "previous_setup_line",
    "previous_result_line",
    "potential_direction",
    "confirmation_required",
    "conditional_invalidation",
    "tp_liquidity_target",
)


def _s(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return str(v)
    return str(v).strip()


def _contains_forbidden(text: str) -> bool:
    low = text.lower()
    for bad in FORBIDDEN_SUBSTRINGS:
        if bad.lower() in low:
            return True
    for rx in FORBIDDEN_REGEX:
        if rx.search(text):
            return True
    return False


def sanitize_trader_text(text: str) -> str:
    if not text:
        return ""
    out = text
    for bad in FORBIDDEN_SUBSTRINGS:
        out = re.sub(re.escape(bad), "", out, flags=re.IGNORECASE)
    for rx in FORBIDDEN_REGEX:
        out = rx.sub("", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def drop_forbidden_lines(text: str) -> str:
    kept: list[str] = []
    for line in text.splitlines():
        if _contains_forbidden(line):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def assert_report_clean(report_fa: str) -> None:
    """Raise AssertionError in tests; production uses drop_forbidden_lines."""
    if _contains_forbidden(report_fa):
        raise AssertionError("report_fa contains forbidden internal tokens")


def is_report_clean(report_fa: str) -> bool:
    return not _contains_forbidden(report_fa)


def prepare_structured_for_user_report(structured: dict[str, Any]) -> dict[str, Any]:
    """Strip model audit prose; keep trader fields only (sanitized)."""
    data = deepcopy(structured)
    raw_model_report = data.pop("report_fa", None)
    artifacts = data.setdefault("_artifacts", {})
    if isinstance(artifacts, dict):
        internal = artifacts.setdefault("internal", {})
        if isinstance(internal, dict) and raw_model_report:
            internal["model_report_fa_raw"] = _s(raw_model_report)[:4000]

    for key in TRADER_STRING_KEYS:
        if key in data and isinstance(data[key], str):
            data[key] = sanitize_trader_text(data[key])

    for key in ("no_trade_reasons", "waiting_for"):
        items = data.get(key)
        if isinstance(items, list):
            data[key] = [sanitize_trader_text(_s(x)) for x in items if sanitize_trader_text(_s(x))]

    prev = _s(data.get("previous_setups_review_fa"))
    if prev and _contains_forbidden(prev):
        data["previous_setups_review_fa"] = ""
    line = _s(data.get("previous_setup_line"))
    result = _s(data.get("previous_result_line"))
    if line or result:
        data["previous_setups_review_fa"] = "\n".join(
            x for x in (line, result) if x
        )
    return data


def _entry_zone(data: dict[str, Any], *, conditional: bool = False) -> str:
    if conditional:
        lo = data.get("conditional_entry_zone_low")
        hi = data.get("conditional_entry_zone_high")
    else:
        lo = data.get("entry_zone_low")
        hi = data.get("entry_zone_high")
    ep = data.get("entry_price")
    if lo is not None and hi is not None:
        return f"{_s(lo)} - {_s(hi)}"
    if not conditional:
        lo = data.get("conditional_entry_zone_low")
        hi = data.get("conditional_entry_zone_high")
        if lo is not None and hi is not None:
            return f"{_s(lo)} - {_s(hi)}"
    if ep is not None:
        return _s(ep)
    return ""


def _has_conditional_plan(data: dict[str, Any]) -> bool:
    mode = _s(data.get("setup_mode")).lower()
    if mode == "conditional":
        return True
    pot = _s(data.get("potential_direction")).upper()
    if pot in ("LONG", "SHORT"):
        return True
    final = _s(data.get("final_ai_decision") or "").upper().replace("_", " ")
    return final == "WAIT"


def _truncate_words(text: str, max_words: int = MAX_REPORT_WORDS) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]) + "…"


def _format_bias(data: dict[str, Any]) -> str:
    raw = data.get("market_bias") or data.get("bias") or ""
    cleaned = sanitize_trader_text(_s(raw))
    if not cleaned:
        return "—"
    low = cleaned.lower()
    if "bull" in low:
        return "Bullish"
    if "bear" in low:
        return "Bearish"
    return cleaned[:80]


def build_trader_report_fa(data: dict[str, Any], *, symbol: str = "BTCUSDT") -> str:
    trade_decision = _s(data.get("decision") or "NO TRADE").upper().replace("_", " ")
    final_decision = _s(data.get("final_ai_decision") or trade_decision).upper().replace("_", " ")
    price = data.get("current_price") or data.get("reference_price")

    lines: list[str] = [
        f"{symbol} ICT Analysis",
        "",
        "Price:",
        _s(price) or "—",
        "",
        "Bias:",
        _format_bias(data),
        "",
        "Decision:",
        final_decision,
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
        if data.get("take_profit_2") is not None:
            lines.extend(["TP2:", _s(data.get("take_profit_2")), ""])
        lines.extend(["RR:", _s(data.get("risk_reward")) or "—", ""])

        bullets = [
            data.get("ict_liquidity") or data.get("liquidity"),
            data.get("ict_structure") or data.get("mss_bos"),
            data.get("ict_displacement") or data.get("displacement"),
            data.get("ict_fvg") or data.get("fvg"),
            data.get("ict_order_block") or data.get("order_blocks"),
        ]
        lines.extend(["ICT Reason:", ""])
        for val in bullets:
            text = sanitize_trader_text(_s(val))
            if text and not _contains_forbidden(text):
                lines.append(f"- {text}")
        lines.append("")

        confirm = sanitize_trader_text(_s(data.get("entry_confirmation") or data.get("entry_trigger")))
        if confirm and not _contains_forbidden(confirm):
            lines.extend(["Entry Confirmation:", confirm, ""])

        inv = sanitize_trader_text(_s(data.get("invalidation_condition") or data.get("invalidation")))
        if inv and not _contains_forbidden(inv):
            lines.extend(["Invalidation:", inv, ""])

    elif _has_conditional_plan(data):
        pot = _s(data.get("potential_direction")).upper()
        if pot not in ("LONG", "SHORT"):
            pot = "—"
        lines.extend(
            [
                "Conditional Setup:",
                "",
                "Potential Direction:",
                pot,
                "",
                "Entry Zone:",
                _entry_zone(data, conditional=True) or "—",
                "",
            ]
        )
        confirm = sanitize_trader_text(
            _s(data.get("confirmation_required") or data.get("entry_confirmation"))
        )
        if confirm and not _contains_forbidden(confirm):
            lines.extend(["Confirmation required:", confirm, ""])
        inv = sanitize_trader_text(
            _s(data.get("conditional_invalidation") or data.get("invalidation_condition") or data.get("invalidation"))
        )
        if inv and not _contains_forbidden(inv):
            lines.extend(["Invalidation:", inv, ""])
        if data.get("stop_loss") is not None:
            lines.extend(["Planned SL:", _s(data.get("stop_loss")), ""])
        if data.get("take_profit") is not None:
            lines.extend(["Planned TP1:", _s(data.get("take_profit")), ""])
        if data.get("risk_reward"):
            lines.extend(["Planned RR:", _s(data.get("risk_reward")), ""])

        bullets = [
            data.get("ict_liquidity") or data.get("liquidity"),
            data.get("ict_structure") or data.get("mss_bos"),
            data.get("ict_displacement") or data.get("displacement"),
            data.get("ict_fvg") or data.get("fvg"),
            data.get("ict_order_block") or data.get("order_blocks"),
        ]
        lines.extend(["ICT Reason:", ""])
        for val in bullets:
            text = sanitize_trader_text(_s(val))
            if text and not _contains_forbidden(text):
                lines.append(f"- {text}")
        lines.append("")

    elif "NO" in trade_decision:
        reasons = data.get("no_trade_reasons") or []
        if isinstance(reasons, list) and reasons:
            lines.append("Reason:")
            for r in reasons[:3]:
                t = sanitize_trader_text(_s(r))
                if t and not _contains_forbidden(t):
                    lines.append(f"- {t}")
            lines.append("")
        waiting = data.get("waiting_for") or []
        if isinstance(waiting, list) and waiting:
            lines.append("Waiting for:")
            for w in waiting[:5]:
                t = sanitize_trader_text(_s(w))
                if t and not _contains_forbidden(t):
                    lines.append(f"- {t}")
            lines.append("")

    prev = sanitize_trader_text(_s(data.get("previous_setups_review_fa")))
    if prev and not _contains_forbidden(prev):
        lines.extend(["Previous Setup Review:", prev, ""])

    report = drop_forbidden_lines("\n".join(lines).strip())
    report = _truncate_words(sanitize_trader_text(report))
    return report


def structured_for_user_display(structured: dict[str, Any] | None) -> dict[str, Any]:
    """Dashboard/API copy: same trading JSON as persisted, without internal-only artifacts."""
    if not structured:
        return {}
    out = deepcopy(structured)
    artifacts = out.get("_artifacts")
    if isinstance(artifacts, dict):
        artifacts = dict(artifacts)
        artifacts.pop("internal", None)
        out["_artifacts"] = artifacts
    return out


def finalize_user_report(
    structured: dict[str, Any],
    *,
    symbol: str = "BTCUSDT",
    live: float = 0,
) -> str:
    """Build user-visible report_fa only.

    Does not mutate trading decisions or levels in ``structured`` (decision, entry, SL, TP).
    Optional ``live`` fills Price in the report when the model omitted ``current_price`` — display only.
    """
    prepared = prepare_structured_for_user_report(structured)
    if live > 0 and not prepared.get("current_price"):
        prepared["current_price"] = round(live, 2)
    report = build_trader_report_fa(prepared, symbol=symbol)
    report = drop_forbidden_lines(report)
    return _truncate_words(report)
