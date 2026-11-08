"""Candle-bound four-hour conditional outlook; never an executable order.

Forecast watch areas are independent of verified plans. The model chooses IDs and a
scenario family, while the server resolves levels, statuses, triggers and targets.
Only compile_plan/monitor can produce Paper orders.
"""
from __future__ import annotations

import math
import re

_ARMENIAN_RE = re.compile(r"[\u0530-\u058F]")

SCENARIOS = {
    "support_bounce": ("LONG", ("support", "OB", "FVG"),
        "رسیدن قیمت به حمایت/تقاضا، واکنش و تأیید برگشت در کندل بستهٔ 5m؛ ورود روی تماس اولیه مجاز نیست.",
        "از دست رفتن کف ناحیه و تثبیت زیر آن سناریوی برگشت را بی‌اعتبار می‌کند."),
    "resistance_rejection": ("SHORT", ("resistance", "OB", "FVG"),
        "رسیدن قیمت به مقاومت/عرضه، واکنش و تأیید برگشت در کندل بستهٔ 5m؛ ورود روی تماس اولیه مجاز نیست.",
        "تثبیت بالای سقف ناحیه سناریوی برگشت را بی‌اعتبار می‌کند."),
    "support_break": ("SHORT", ("support",),
        "بسته‌شدن معتبر 5m زیر کف حمایت، سپس بازآزمایی آن از پایین و حفظ شکست؛ ورود روی شکست اولیه مجاز نیست.",
        "بازگشت و تثبیت قیمت بالای حمایت، سناریوی شکست را بی‌اعتبار می‌کند."),
    "resistance_break": ("LONG", ("resistance",),
        "بسته‌شدن معتبر 5m بالای سقف مقاومت، سپس بازآزمایی آن از بالا و حفظ شکست؛ ورود روی شکست اولیه مجاز نیست.",
        "بازگشت و تثبیت قیمت زیر مقاومت، سناریوی شکست را بی‌اعتبار می‌کند."),
    "trend_pullback": (None, ("support", "resistance", "OB", "FVG"),
        "اصلاح قیمت تا ناحیه، سپس بسته‌شدن تأییدی 5m در جهت ساختار؛ صرف رسیدن قیمت مجوز معامله نیست.",
        "شکست ساختاری ناحیه برخلاف جهت سناریو، آن را بی‌اعتبار می‌کند."),
}

FA_NAMES = {
    "support_bounce": "برگشت احتمالی از حمایت",
    "resistance_rejection": "واکنش احتمالی به مقاومت",
    "support_break": "شکست و بازآزمایی حمایت",
    "resistance_break": "شکست و بازآزمایی مقاومت",
    "trend_pullback": "اصلاح و ادامهٔ روند",
}


def safe_model_narrative_fa(value, *, limit=3000):
    """Reject mixed-script model prose observed in production (notably Armenian fragments)."""
    text = " ".join(str(value or "").split())
    if not text or _ARMENIAN_RE.search(text):
        return ""
    return text[:limit].rstrip()


def _finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _metric(value, decimals=1):
    number = _finite(value)
    if number is None:
        return None
    return f"{number:,.{decimals}f}".rstrip("0").rstrip(".")


def format_market_features_fa(strategy_features, execution_regime):
    """Deterministic Persian fallback from locally computed closed-candle indicators."""
    by_tf = (strategy_features or {}).get("timeframes") or {}
    regime_frames = (execution_regime or {}).get("timeframes") or {}
    regime_names = {"bullish": "صعودی", "bearish": "نزولی", "range": "رنج",
                    "ranging": "رنج", "unclear": "نامشخص"}
    relation_names = {"above": "EMA9 بالای EMA20", "below": "EMA9 زیر EMA20",
                      "equal": "EMA9 برابر EMA20"}
    lines = []
    for tf in ("4h", "1h", "15m", "5m"):
        frame = by_tf.get(tf) or {}
        if not isinstance(frame, dict) or frame.get("available") is not True:
            continue
        parts = []
        structural = (regime_frames.get(tf) or {}).get("trend")
        if structural in regime_names:
            parts.append("ساختار " + regime_names[structural])
        relation = frame.get("ema9_ema20_relation")
        if relation in relation_names:
            parts.append(relation_names[relation])
        for key, label, decimals in (
            ("rsi14", "RSI14", 1),
            ("adx14", "ADX14", 1),
            ("atr14", "ATR14", 1),
            ("relative_volume20", "RVOL20", 2),
        ):
            value = _metric(frame.get(key), decimals)
            if value is not None:
                parts.append(f"{label}={value}")
        if parts:
            lines.append(f"{tf.upper()}: " + "؛ ".join(parts))
    if not lines:
        return ""
    return "شاخص‌های محاسبه‌شده از کندل‌های بسته:\n" + "\n".join(lines)




def _geometry(item):
    return {k: item.get(k) for k in (
        "id", "kind", "timeframe", "time_ms", "price", "low", "high",
        "direction", "status", "touch_count", "last_touch_ms", "confirmed_ms",
    ) if k in item}


def compile_forecasts(raw, evidence, *, observed_ms=None, limit=4,
                      major_levels=None, min_gap=None):
    """Only independently validated HTF levels can appear in watch-only outlooks.

    This gate is separate from compile_plan. AI selection never upgrades a
    low-timeframe pivot into major support/resistance. A target needs a clear
    full-zone, volatility-scaled path with no nearer *validated* HTF obstacle.
    """
    by_id = {x["id"]: x for x in evidence}
    major_by_id = {x["id"]: x for x in (major_levels or [])}
    forecast, rejected, seen = [], [], set()
    if not isinstance(raw, list):
        return forecast, ["invalid_forecast_collection"]
    if len(raw) > limit:
        rejected.append("forecast_count_exceeded")
    required_gap = float(min_gap) if isinstance(min_gap, (float, int)) else math.inf
    if not math.isfinite(required_gap) or required_gap < 0:
        required_gap = math.inf
    for index, proposal in enumerate(raw[:limit], 1):
        if not isinstance(proposal, dict):
            rejected.append(f"{index}:invalid_forecast")
            continue
        kind = proposal.get("scenario_type")
        rule = SCENARIOS.get(kind) if isinstance(kind, str) else None
        if rule is None:
            rejected.append(f"{index}:invalid_scenario_type")
            continue
        direction = proposal.get("direction")
        if direction not in ("LONG", "SHORT") or (rule[0] and direction != rule[0]):
            rejected.append(f"{index}:invalid_direction")
            continue
        id_ = proposal.get("level_id")
        item = by_id.get(id_) if isinstance(id_, str) else None
        if not item or item.get("kind") not in rule[1]:
            rejected.append(f"{index}:unknown_or_wrong_level")
            continue
        if item.get("timeframe") not in ("1h", "4h") or id_ not in major_by_id:
            rejected.append(f"{index}:not_proven_major_htf_level")
            continue
        if item.get("direction") and item["direction"] != direction:
            rejected.append(f"{index}:level_direction_conflict")
            continue
        if kind == "trend_pullback" and (
            (direction == "LONG" and item["kind"] == "resistance")
            or (direction == "SHORT" and item["kind"] == "support")
        ):
            rejected.append(f"{index}:pullback_level_conflict")
            continue
        target_id = proposal.get("target_id")
        target = by_id.get(target_id) if isinstance(target_id, str) else None
        if target_id is not None:
            if target is None:
                rejected.append(f"{index}:unknown_target")
                continue
            if target.get("timeframe") not in ("1h", "4h") or target_id not in major_by_id:
                rejected.append(f"{index}:target_not_proven_major_htf_level")
                continue
        sign = 1 if direction == "LONG" else -1
        if target is not None:
            if target.get("kind") != ("resistance" if sign == 1 else "support"):
                rejected.append(f"{index}:wrong_target_kind")
                continue
            # Measure the usable distance from the two *near* zone edges.
            # Center-to-center distance mistakenly approved overlapping zones.
            clearance = (
                float(target["low"]) - float(item["high"]) if sign == 1
                else float(item["low"]) - float(target["high"])
            )
            if clearance <= 0:
                rejected.append(f"{index}:target_overlaps_or_behind_source")
                continue
            if clearance < required_gap:
                rejected.append(f"{index}:target_without_htf_room")
                continue
            closer_obstacle = any(
                other["id"] not in (id_, target_id)
                and other["kind"] == ("resistance" if sign == 1 else "support")
                and (
                    float(item["high"]) < float(other["low"]) < float(target["low"])
                    if sign == 1 else
                    float(target["high"]) < float(other["high"]) < float(item["low"])
                )
                for other in (by_id[x] for x in major_by_id if x in by_id)
            )
            if closer_obstacle:
                rejected.append(f"{index}:nearer_major_obstacle")
                continue
        key = (kind, direction, id_)
        if key in seen:
            rejected.append(f"{index}:duplicate_forecast")
            continue
        reason = str(proposal.get("reason_fa") or "").strip()[:500]
        if not reason:
            rejected.append(f"{index}:missing_reason")
            continue
        seen.add(key)
        level = _geometry(item)
        level["major_evidence"] = {
            k: major_by_id[id_].get(k) for k in
            ("proof", "reaction_atr4h", "confluence_count", "quality_score")
        }
        forecast.append({
            "scenario_type": kind, "direction": direction,
            "level": level, "target": _geometry(target) if target else None,
            "condition_fa": rule[2], "invalidation_fa": rule[3],
            "reason_fa": reason,
            "paper_order": False, "watchlist_only": True,
            "as_of_ms": observed_ms,
            "valid_until_ms": observed_ms + 4 * 60 * 60 * 1000 if isinstance(observed_ms, int) else None,
        })
    return forecast, rejected


def format_major_levels_fa(levels):
    """Always report the defensible structural areas, even when no forecast survived."""
    if not levels:
        return ("سطوح اصلی: در داده‌های بستهٔ فعلی 1H/4H هیچ حمایت یا مقاومتی "
                "با شواهد ساختاری کافی تأیید نشد؛ سطح 5m/15m جایگزین نمی‌شود.")
    lines = ["حمایت و مقاومت اصلی 1H/4H (شناسایی ساختاری؛ نه سیگنال ورود):"]
    for role, name in (("support", "حمایت"), ("resistance", "مقاومت")):
        selected = [x for x in levels if x.get("role") == role][:3]
        for item in selected:
            reaction = item.get("reaction_atr4h")
            proof = "سوئینگ تأییدشده" if item.get("proof") == "confirmed_unbroken_swing" else "اوردر بلاکِ شکست ساختار"
            lines.append(
                f"{name} {item['timeframe'].upper()}: {item['low']:,.1f} تا {item['high']:,.1f} "
                f"| {proof} | واکنش بسته‌شده {reaction:g} برابر ATR چهار‌ساعته"
                f" | هم‌پوشانی مستقل: {item.get('confluence_count', 0)}"
            )
    lines.append("اعتبار این نواحی تضمین واکنش قیمت نیست و با بسته‌شدن معتبر در سمت مخالف بازبینی می‌شود.")
    return "\n".join(lines)


def _fallback_forecast_reason(entry):
    zone = entry.get("level") or {}
    kind = {"support": "حمایت", "resistance": "مقاومت",
            "OB": "اوردر بلاک", "FVG": "ناحیه عدم تعادل قیمت"}.get(
                str(zone.get("kind") or ""), "ناحیه قیمتی")
    timeframe = str(zone.get("timeframe") or "نامشخص")
    status = str(zone.get("status") or "").lower()
    parts = [f"{kind} در تایم‌فریم {timeframe} شناسایی شده است"]
    if status == "fresh":
        parts.append("پس از تأیید هنوز برخورد مجزایی نداشته و دست‌نخورده است")
    elif status == "retested":
        parts.append("پس از تأیید بازآزمایی شده است")
    elif status == "filled":
        parts.append("ناحیه قبلاً پر شده است")
    return "؛ ".join(parts) + "."


def _status_fa(value):
    return {"fresh": "دست‌نخورده", "retested": "بازآزمایی‌شده",
            "filled": "پرشده", "invalidated": "بی‌اعتبارشده"}.get(
                str(value or "").lower(), str(value or "نامشخص"))


def format_forecasts_fa(forecasts):
    """Persian watchlist text; invalid model-language fragments fall back to evidence facts."""
    if not forecasts:
        return "چشم‌انداز مشروط چهار‌ساعته: در این نوبت سناریوی قابل دفاعی با سطح و مقصد معتبر پذیرفته نشد؛ سطوح اصلی در بخش بالا آمده‌اند."
    lines = ["چشم‌انداز چهار‌ساعته ـ سناریوهای مشروط (فقط تحت نظر؛ نه دستور ورود):"]
    until = forecasts[0].get("valid_until_ms")
    if isinstance(until, int):
        from datetime import datetime, timezone
        label = datetime.fromtimestamp(until / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")
        lines.append(f"اعتبار این سناریوها تا {label} UTC؛ پس از آن نیازمند تحلیل تازه هستند.")
    for idx, entry in enumerate(forecasts, 1):
        zone = entry.get("level") or {}
        low, high = zone.get("low"), zone.get("high")
        kind = entry.get("scenario_type")
        direction = "لانگ" if entry.get("direction") == "LONG" else "شورت"
        condition = f"{idx}) {FA_NAMES.get(kind, 'سناریوی مشروط')} ({direction}) | {zone.get('timeframe', '—')} | ناحیه {low:g} تا {high:g}"
        if zone.get("status") is not None:
            condition += f" | وضعیت: {_status_fa(zone.get('status'))}"
        if zone.get("touch_count") is not None:
            condition += f" | تعداد برخوردهای مجزا: {zone['touch_count']}"
        reason = safe_model_narrative_fa(entry.get("reason_fa"), limit=500) or _fallback_forecast_reason(entry)
        trigger = safe_model_narrative_fa(entry.get("condition_fa"), limit=300)
        invalidation = safe_model_narrative_fa(entry.get("invalidation_fa"), limit=300)
        lines.extend((
            condition,
            f"شواهد: {reason}",
            f"شرط: {trigger or 'ورود فقط پس از تأیید کندل بستهٔ 5m؛ لمس اولیه مجوز ورود نیست.'}",
        ))
        target = entry.get("target")
        if isinstance(target, dict):
            lines.append(
                f"مقصد مشروط: {target['low']:g} تا {target['high']:g} ({target.get('timeframe', '—')})."
            )
        else:
            lines.append("مقصد: بدون سطح مقصد معتبر در داده‌های انتخاب‌شده.")
        lines.append(
            f"ابطال: {invalidation or 'تثبیت قیمت در سمت مخالف ناحیه، سناریو را بی‌اعتبار می‌کند.'}"
        )
    lines.append("هیچ سناریوی تحت نظر، بدون قرارداد اجرایی مستقل و تأیید کندلی، سفارش Paper ایجاد نمی‌کند.")
    return "\n".join(lines)

