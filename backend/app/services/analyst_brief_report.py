"""Concise, deterministic Persian display for the 4H multi-strategy analyst.

Presentation ONLY. Do not infer entries, order placement, fills or target prices
from prose. The full candle, indicator, feasibility and strategy evidence remains
in the saved structured analysis and API payload.
"""
from __future__ import annotations

import math

from app.services.ai_trader_report import drop_forbidden_lines, sanitize_trader_text

_LABELS = {
    "ict_reversal": "A · برگشت ICT",
    "trend_continuation": "B · ادامه‌روند",
    "breakout_retest": "C · شکست و بازآزمایی",
}
_REGIMES = {
    "bullish": "صعودی",
    "bearish": "نزولی",
    "range": "رنج",
    "ranging": "رنج",
    "unclear": "نامشخص",
}
_DIRECTIONS = {"LONG": "LONG", "SHORT": "SHORT"}


def _price(value: object) -> str:
    """Report at BTCUSDT's 0.1 precision; never round stored execution levels."""
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return "—"
    return f"{value:,.1f}" if math.isfinite(value) and value > 0 else "—"


def _zone(low: object, high: object) -> str:
    lo, hi = _price(low), _price(high)
    if "—" in (lo, hi):
        return "—"
    return lo if lo == hi else f"{lo} تا {hi}"


def _brief_condition(value: object, max_length: int = 190) -> str:
    safe = sanitize_trader_text(" ".join(str(value or "").split()))
    if not safe:
        return "طبق تأیید کندلیِ مستقل موتور مانیتور"
    if len(safe) <= max_length:
        return safe
    # Keep the first coherent phrase instead of spilling a whole analysis.
    short = safe[:max_length]
    for separator in ("؛", ".", "،"):
        if separator in short:
            short = short.rsplit(separator, 1)[0]
            break
    return short.rstrip() + "…"


def _plans(structured: dict) -> dict[str, dict]:
    plans = [structured, *((structured.get("strategy_candidates") or {}).values())]
    result = {}
    for plan in plans:
        if not isinstance(plan, dict):
            continue
        sid = plan.get("strategy_id")
        if sid in _LABELS and plan.get("setup_mode") == "conditional" and (
            plan.get("potential_direction") in _DIRECTIONS
        ):
            result[sid] = plan
    return result


def compact_analyst_report_fa(structured: dict, *, live: float = 0) -> str:
    """Render a short analyst report while retaining all analysis data verbatim."""
    summary = structured.get("analyst_summary") or {}
    frames = (summary.get("execution_regime") or {}).get("timeframes") or {}
    h4 = _REGIMES.get((frames.get("4h") or {}).get("trend"), "نامشخص")
    h1 = _REGIMES.get((frames.get("1h") or {}).get("trend"), "نامشخص")
    plans = _plans(structured)
    fallback_sids = {
        item.get("strategy_id")
        for item in summary.get("execution_fallbacks") or []
        if isinstance(item, dict)
    }
    lines = [
        "BTC/USDT | تحلیل چهار‌ساعته",
        f"روند: 4H {h4} · 1H {h1}",
    ]
    reference = live if live > 0 else structured.get("current_price")
    if _price(reference) != "—":
        lines.append(f"قیمت زمان تحلیل: {_price(reference)}")
    levels = summary.get("outlook_major_levels") or []
    # Display one independent main area per role; keep the complete catalog
    # unchanged for the AI, monitor and expanded technical details.
    areas = []
    for role, label in (("support", "حمایت"), ("resistance", "مقاومت")):
        item = next((i for i in levels if isinstance(i, dict)
                     and i.get("role") == role), None)
        if item:
            areas.append(
                f"{label} {str(item.get('timeframe', '')).upper()} "
                + _zone(item.get("low"), item.get("high"))
            )
    lines.append("سطوح اصلی: " + (" | ".join(areas) if areas else "سطح عمدهٔ تأییدشده‌ای در فهرست گزارش نیست"))

    forecasts = summary.get("forecast_scenarios") or []
    if forecasts and isinstance(forecasts[0], dict):
        item = forecasts[0]
        level = item.get("level") or {}
        direction = _DIRECTIONS.get(item.get("direction"), "—")
        target = item.get("target")
        target_desc = (_zone(target.get("low"), target.get("high"))
                       if isinstance(target, dict) else "بدون مقصد ساختاری معتبر")
        label = {
            "resistance_rejection": "رد مقاومت",
            "support_bounce": "بازگشت از حمایت",
            "breakout_retest": "شکست و بازآزمایی",
            "trend_continuation": "ادامه‌روند",
        }.get(item.get("scenario_type"), "سناریوی مشروط")
        lines.append(
            f"چشم‌انداز مشروط چهار‌ساعته (فقط پایش): {label} {direction} "
            f"در {_zone(level.get('low'), level.get('high'))}؛ مقصد: {target_desc}"
        )
    else:
        lines.append("چشم‌انداز مشروط چهار‌ساعته: سناریوی دارای مقصد معتبر ثبت نشده است.")

    lines.append("")
    lines.append("وضعیت A/B/C:")
    for sid, name in _LABELS.items():
        plan = plans.get(sid)
        if plan is None:
            lines.append(f"• {name}: بدون پلن اجرایی")
        else:
            direction = plan["potential_direction"]
            origin = "ساختاریِ سرور" if sid in fallback_sids else "پیشنهاد AI"
            lines.append(f"• {name}: پلن مشروط {direction} ({origin})")

    lines.append("")
    if not plans:
        lines.append("پلن اجرایی: هیچ پلن معتبر Entry / SL / TP صادر نشده است.")
    else:
        lines.append("پلن‌های اجرایی مشروط (هنوز سفارش یا پوزیشن بازشده نیستند):")
        # Preserve the original analyst rank rather than making B always first.
        for sid, plan in sorted(plans.items(),
                                key=lambda pair: int(pair[1].get("analyst_rank") or 99)):
            direction = plan["potential_direction"]
            lines.append(f"{_LABELS[sid]} | {direction} | WAIT")
            lines.append(
                "Entry: " + _zone(plan.get("entry_zone_low"), plan.get("entry_zone_high"))
                + " | SL: " + _price(plan.get("stop_loss"))
                + " | TP: " + _price(plan.get("take_profit"))
            )
            lines.append(
                "شرط: " + _brief_condition(
                    plan.get("confirmation_required") or plan.get("entry_confirmation"))
            )
            if plan.get("trade_against_dominant_trend") is True:
                lines.append("هشدار: خلاف روند غالب")
            if sid == "trend_continuation":
                lines.append("B: امکان ثبت Limit روی پولبک معتبر؛ Fill فقط با لمس بعدی قیمت")
    return drop_forbidden_lines("\n".join(lines)).strip()
