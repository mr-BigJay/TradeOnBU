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

    from app.services.chart_patterns import NAMES, POSITIONS
    for pattern in summary.get("chart_patterns") or []:
        lines.append(f"{NAMES[pattern['kind']]} ({pattern['timeframe']}) | {POSITIONS[pattern['position']]} | "
                     f"کف {_price(pattern['lower']['price'])}، سقف {_price(pattern['upper']['price'])}")
    lines.extend(_scenario_lines(summary))
    prior = summary.get("previous_review")
    if prior:
        lines.append(f"پیگیری تحلیل قبل | {_zone(prior['level']['low'], prior['level']['high'])} | {prior['status_fa']}")
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


def telegram_analyst_brief(structured, *, symbol, live, analyzed_at):
    """Four-hour reader view, using only the primary plan for prices."""
    import re
    from app.domain.tehran_time import format_tehran
    from app.services.telegram_message_format import format_persian_report_for_telegram

    summary = structured["analyst_summary"]
    lines = [f"نماد {symbol} | زمان تهران {format_tehran(analyzed_at)}", f"قیمت زمان تحلیل {_price(live)}"]
    lines.extend(_scenario_lines(summary))
    prior = summary.get("previous_review") or {}
    if prior.get("status_fa"):
        level = prior.get("level") or {}
        lines.append(f"پیگیری ناحیهٔ {_zone(level.get('low'), level.get('high'))} | {prior['status_fa']}")
    frames = (summary.get("execution_regime") or {}).get("timeframes") or {}
    trend = [_REGIMES.get((frames.get(tf) or {}).get("trend"), "نامشخص") for tf in ("4h","1h")]
    lines.append(f"روند چهارساعته {trend[0]} و روند یک‌ساعته {trend[1]} است.")
    narrative = summary.get("market_summary_fa") or ""
    # Only complete short sentences; a decimal point is not a sentence ending.
    def excerpt(text, budget):
        selected = []
        for sentence in re.split(r"(?<=[.!؟])\s+", " ".join(str(text or "").split())):
            if not sentence.endswith((".", "!", "؟")) or sum(map(len, selected))+len(sentence)>budget:
                break
            selected.append(sentence)
            if len(selected)==3:
                break
        return " ".join(selected)

    outlook = excerpt(narrative, 650)
    if outlook:
        lines.append(outlook)
    from app.services.chart_patterns import NAMES, POSITIONS
    memory = summary.get("drawing_memory") or {}
    if "channel_proposals" in memory and not any(p["kind"].endswith("_channel") for p in memory["active"]):
        lines.append("کانال پیشنهادی با کندل‌ها تأیید نشد." if memory["channel_proposals"]
                     else "در این نوبت تحلیلگر کانال تازه‌ای پیشنهاد نکرد و کانال فعالی ثبت نشده است.")
    primary_channel = next((p for p in memory.get("active", []) if p["timeframe"] == "4h"
                            and p["kind"].endswith("_channel")), None)
    if primary_channel:
        start = min(primary_channel[side]["anchors"][0][0] for side in ("upper","lower"))
        days = (primary_channel["as_of_ms"] - start) / 86_400_000
        lines.append(f"کانال اصلی چهارساعته از نخستین نقطهٔ مبنا حدود {days:.1f} روز امتداد دارد.")
        if primary_channel.get("break_status"):
            lines.append("قیمت در بسته‌شدن ساعتی از کانال عبور کرده و در انتظار تأیید چهارساعته است.")
    for ended in (summary.get("drawing_memory") or {}).get("ended", [])[:2]:
        event = {"above":"کندل بالای سقف بسته شد", "below":"کندل زیر کف بسته شد",
                 "converged":"دو مرز به هم رسیدند"}[ended["position"]]
        lines.append(f"پیگیری {NAMES[ended['kind']]} ({ended['timeframe']})؛ {event}.")
    for pattern in (summary.get("chart_patterns") or [])[:3]:
        lines.append(f"{NAMES[pattern['kind']]} ({pattern['timeframe']})، {POSITIONS[pattern['position']]}؛ "
                     f"بین {_price(pattern['lower']['price'])} و {_price(pattern['upper']['price'])}.")
    plans = _plans(structured)
    primary = plans.get(structured.get("strategy_id"))
    records = (structured.get("_artifacts") or {}).get("strategy_persistence") or {}
    for sid, label in _LABELS.items():
        label = f"استراتژی {label}"
        plan = plans.get(sid)
        if not plan:
            lines.append(f"{label} | پلن مشروطی ثبت نشده.")
            continue
        direction = "لانگ" if plan["potential_direction"] == "LONG" else "شورت"
        reason = excerpt(plan.get("trade_reason"), 230)
        if not reason:
            reason = {"ict_reversal":"سناریو به جارو شدن سطح و بازپس‌گیری آن وابسته است.",
                      "trend_continuation":"ورود روی اصلاح به ناحیهٔ پیشنهادی در جهت روند بررسی می‌شود.",
                      "breakout_retest":"پس از تأیید شکست، برگشت به همان مرز برای ورود بررسی می‌شود."}[sid]
        lines.append(f"{label} | {'پلن ورود منتخب، ' if plan is primary else ''}{direction} مشروط. {reason}")
    if primary and "hourly_watch" not in summary:
        record = records.get(primary["strategy_id"]) or {}
        levels = record.get("chart_levels") or {}
        entry = levels.get("entry", primary.get("entry_price"))
        stop = levels.get("stop_loss", primary.get("stop_loss"))
        target = levels.get("take_profit", primary.get("take_profit"))
        lines.append("مسیر مشروط از قیمت فعلی تا ناحیهٔ ورود است؛ در صورت واکنش هم‌جهت، مقصد بعدی هدف پلن است.")
        lines.append(f"ورود {_price(entry)} | حد ضرر {_price(stop)} | هدف {_price(target)}")
        if all(_price(x) != "—" for x in (entry,stop,target)) and float(entry) != float(stop):
            rr = abs((float(target)-float(entry))/(float(entry)-float(stop)))
            lines.append(f"ریسک به بازده پیش از هزینه‌ها ۱ به {rr:.2f}")
        record = records.get(primary["strategy_id"]) or {}
        state = record.get("lifecycle_state")
        statuses = {"PENDING_ENTRY":"سفارش آزمایشی در انتظار پرشدن است.", "POSITION_OPEN":"پوزیشن آزمایشی باز است.",
                    "POSITION_CLOSED":"پوزیشن آزمایشی بسته شده است.", "EXPIRED":"ایده منقضی شده است.",
                    "INVALIDATED":"ایده باطل شده است."}
        if state in statuses:
            lines.append(statuses[state])
        elif record.get("skipped"):
            lines.append("در این نوبت ستاپ تازه‌ای ثبت نشد.")
        else:
            lines.append("در صورت فعال‌شدن پلن و پرشدن سفارش در ناحیهٔ ورود، هدف بالا پیگیری می‌شود.")
    else:
        forecasts = summary.get("forecast_scenarios") or []
        if forecasts:
            area = forecasts[0].get("level") or {}
            lines.append(f"ناحیهٔ تحت نظر برای تحلیل بعدی: {_zone(area.get('low'),area.get('high'))}")
    return format_persian_report_for_telegram("\n".join(lines))


def _scenario_lines(summary):
    lines = []
    scenario = summary.get("market_scenario") or {}
    if scenario.get("broken"):
        broken = scenario["broken"]
        lines.append(f"ابطال سناریوی قبلی با بسته‌شدن کندل چهارساعته در {_price(broken['close'])} و عبور از {_price(broken['level'])} در {broken['time']}.")
    if scenario.get("direction"):
        label = "نزولی" if scenario["direction"] == "SHORT" else "صعودی"
        state = "ادامه دارد" if scenario["status"] == "continued" else "ثبت شد"
        side = "بالای" if label == "نزولی" else "زیر"
        lines.append(f"سناریوی اصلی {label} {state}. ابطال با بسته‌شدن کندل چهارساعته {side} {_price(scenario['level'])}.")
    elif scenario:
        lines.append("جهت ساختاری پایدار هنوز مشخص نشده است.")
    event = scenario.get("event") or {}
    if event.get("status") == "pause":
        lines.append(f"توقف پلن تازه به دلیل رویداد ثبت‌شده «{event['title']}» — {event['source']}")
    elif event.get("status") == "unavailable":
        lines.append("پوشش خبر و تقویم اقتصادی در این بررسی در دسترس نیست.")
    if event.get("news_available") is False and event.get("calendar_available"):
        lines.append("پوشش خبرهای جهان در این نوبت در دسترس نیست.")
    for plan in (summary.get("hourly_watch") or {}).values():
        lines.append(f"رصد ساعتی {plan['strategy']} · {plan['direction']} در "
                     f"{_zone(plan['zone_low'],plan['zone_high'])} | {plan['expect_fa']}")
    return lines
