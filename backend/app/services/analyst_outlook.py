"""Candle-bound four-hour conditional outlook; never an executable order.

Forecast watch areas are independent of verified plans. The model chooses IDs and a
scenario family, while the server resolves levels, statuses, triggers and targets.
Only compile_plan/monitor can produce Paper orders.
"""
from __future__ import annotations

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


def _geometry(item):
    return {k: item.get(k) for k in (
        "id", "kind", "timeframe", "time_ms", "price", "low", "high",
        "direction", "status", "touch_count", "last_touch_ms", "confirmed_ms",
    ) if k in item}


def compile_forecasts(raw, evidence, *, observed_ms=None, limit=4):
    """Return accepted, rejected. Invalid model IDs/text cannot become price levels."""
    by_id = {x["id"]: x for x in evidence}
    forecast, rejected, seen = [], [], set()
    if not isinstance(raw, list):
        return forecast, ["invalid_forecast_collection"]
    if len(raw) > limit:
        rejected.append("forecast_count_exceeded")
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
        if item.get("direction") and item["direction"] != direction:
            rejected.append(f"{index}:level_direction_conflict")
            continue
        if kind == "trend_pullback" and (
            (direction == "LONG" and item["kind"] == "resistance")
            or (direction == "SHORT" and item["kind"] == "support")
        ):
            rejected.append(f"{index}:pullback_level_conflict")
            continue
        if kind == "support_bounce" and item["kind"] in ("OB", "FVG") and direction != "LONG":
            rejected.append(f"{index}:level_direction_conflict")
            continue
        if kind == "resistance_rejection" and item["kind"] in ("OB", "FVG") and direction != "SHORT":
            rejected.append(f"{index}:level_direction_conflict")
            continue
        target_id = proposal.get("target_id")
        target = by_id.get(target_id) if isinstance(target_id, str) else None
        if target_id is not None and target is None:
            rejected.append(f"{index}:unknown_target")
            continue
        sign = 1 if direction == "LONG" else -1
        if target is not None:
            if target.get("kind") != ("resistance" if sign == 1 else "support"):
                rejected.append(f"{index}:wrong_target_kind")
                continue
            if sign * (float(target["price"]) - float(item["price"])) <= 0:
                rejected.append(f"{index}:target_wrong_side")
                continue
        key = (kind, direction, id_)
        if key in seen:
            rejected.append(f"{index}:duplicate_forecast")
            continue
        seen.add(key)
        reason = str(proposal.get("reason_fa") or "").strip()[:500]
        if not reason:
            rejected.append(f"{index}:missing_reason")
            continue
        forecast.append({
            "scenario_type": kind, "direction": direction,
            "level": _geometry(item), "target": _geometry(target) if target else None,
            "condition_fa": rule[2], "invalidation_fa": rule[3],
            "reason_fa": reason,
            "paper_order": False, "watchlist_only": True,
            "as_of_ms": observed_ms,
            "valid_until_ms": observed_ms + 4 * 60 * 60 * 1000 if isinstance(observed_ms, int) else None,
        })
    return forecast, rejected


def format_forecasts_fa(forecasts):
    """Forecast text is deliberately separate from verified entry/SL/TP contracts."""
    if not forecasts:
        return "چشم‌انداز مشروط چهار‌ساعته: ناحیهٔ مستند کافی برای سناریوی قابل نمایش انتخاب نشده است."
    lines = ["چشم‌انداز چهار‌ساعته ـ سناریوهای مشروط (فقط تحت نظر؛ نه دستور ورود):"]
    until = forecasts[0].get("valid_until_ms")
    if isinstance(until, int):
        from datetime import datetime, timezone
        label = datetime.fromtimestamp(until / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")
        lines.append(f"اعتبار این سناریوها تا {label} UTC؛ پس از آن نیازمند تحلیل تازه هستند.")
    for idx, entry in enumerate(forecasts, 1):
        zone = entry["level"]
        low, high = zone["low"], zone["high"]
        kind = entry["scenario_type"]
        direction = "لانگ" if entry["direction"] == "LONG" else "شورت"
        condition = f"{idx}) {FA_NAMES[kind]} ({direction}) | {zone['timeframe']} | ناحیه {low:g} تا {high:g}"
        if "status" in zone:
            condition += f" | وضعیت: {zone['status']}"
        if zone.get("touch_count") is not None:
            condition += f" | تعداد برخوردهای مجزا: {zone['touch_count']}"
        lines.extend((condition, f"شواهد: {entry['reason_fa']}",
                      f"شرط: {entry['condition_fa']}"))
        if entry["target"]:
            target = entry["target"]
            lines.append(f"مقصد مشروط: {target['low']:g} تا {target['high']:g} ({target['timeframe']}).")
        else:
            lines.append("مقصد: بدون سطح مقصد معتبر در داده‌های انتخاب‌شده.")
        lines.append(f"ابطال: {entry['invalidation_fa']}")
    lines.append("هیچ سناریوی تحت نظر، بدون قرارداد اجرایی مستقل و تأیید کندلی، سفارش Paper ایجاد نمی‌کند.")
    return "\n".join(lines)
