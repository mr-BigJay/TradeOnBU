"""Presentation-only Telegram summary; analyst outlook is not an executable order."""
from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any

from app.domain.execution_tp_cap import EXECUTION_MAX_REWARD_R_MULTIPLE
from app.domain.tehran_time import format_tehran
from app.services.analyst_outlook import (
    format_market_features_fa,
    safe_model_narrative_fa,
)
from app.services.telegram_message_format import format_persian_report_for_telegram


def _short(value: Any, limit: int) -> str:
    if limit <= 0:
        return ""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit - 1].rsplit(" ", 1)[0] + "…"


def _price(value: Any) -> str:
    try:
        number = float(value)
        if math.isfinite(number) and number > 0:
            return f"{number:,.2f}".rstrip("0").rstrip(".")
    except (ValueError, TypeError):
        pass
    return "—"


def _regime_fa(value: Any) -> str:
    return {
        "bearish": "نزولی",
        "bullish": "صعودی",
        "range": "رنج",
        "ranging": "رنج",
        "unclear": "نامشخص",
    }.get(str(value or "").lower(), "نامشخص")


def _context(s: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], int | None]:
    raw = s.get("analyst_summary")
    summary = raw if isinstance(raw, dict) else {}
    forecasts = summary.get("forecast_scenarios")
    forecasts = [x for x in forecasts if isinstance(x, dict)] if isinstance(forecasts, list) else []
    accepted = summary.get("accepted")
    return summary, forecasts, accepted if isinstance(accepted, int) else None


def _header_lines(
    *,
    symbol: str,
    live: float,
    analyzed_at: datetime,
    structured: dict[str, Any],
    validation: dict[str, Any],
) -> tuple[list[str], dict[str, Any], list[dict[str, Any]]]:
    s = structured
    summary, forecasts, accepted = _context(s)
    final = str(s.get("final_ai_decision") or s.get("decision") or "NO_TRADE").upper()
    conditional = s.get("setup_mode") == "conditional" or final == "WAIT"
    direction = str(s.get("potential_direction") if conditional else final).upper()
    direction_fa = {"LONG": "لانگ", "SHORT": "شورت"}.get(direction, "نامشخص")
    rejected = final in ("LONG", "SHORT") and not validation.get("ok")

    if rejected:
        status = "رد اعتبارسنجی؛ ورود مجاز نیست"
    elif conditional:
        status = f"انتظار برای {direction_fa}؛ ورود هنوز تأیید نشده"
    elif final in ("LONG", "SHORT"):
        status = f"پیشنهاد {direction_fa}؛ اجرای ورود مشروط به کنترل‌های سامانه"
    elif forecasts:
        status = f"عدم ورود؛ {len(forecasts)} سناریوی مشروط برای رصد"
    elif accepted == 0:
        status = "عدم ورود؛ پلن اجرایی معتبر پذیرفته نشد"
    else:
        status = "عدم ورود؛ سناریوی اجرایی فعال وجود ندارد"

    if accepted == 0:
        strategy = "بدون استراتژی پذیرفته‌شده"
    else:
        strategy = {
            "ict_reversal": "A · برگشت قیمت",
            "trend_continuation": "B · ادامه روند",
            "breakout_retest": "C · شکست و بازآزمایی",
        }.get(s.get("strategy_id"), "ICT")

    lines = [
        f"{_short(symbol, 20)} | {strategy}",
        status,
        f"قیمت: {_price(live)} | زمان تهران: {format_tehran(analyzed_at)}",
    ]

    execution = summary.get("execution_regime")
    frames = execution.get("timeframes") if isinstance(execution, dict) else {}
    frames = frames if isinstance(frames, dict) else {}
    if frames.get("4h") or frames.get("1h"):
        four_hour = _regime_fa((frames.get("4h") or {}).get("trend"))
        one_hour = _regime_fa((frames.get("1h") or {}).get("trend"))
        lines.append(f"روند ساختاری: 4H {four_hour} | 1H {one_hour}")
    else:
        regime = _regime_fa(s.get("market_regime"))
        lines.append(
            f"روند غالب: {regime}" +
            ("؛ سناریو خلاف روند" if s.get("trade_against_dominant_trend") else "")
        )
    return lines, summary, forecasts


def _fallback_forecast_reason(zone: dict[str, Any]) -> str:
    kind = {
        "support": "حمایت",
        "resistance": "مقاومت",
        "OB": "اوردر بلاک",
        "FVG": "ناحیه عدم تعادل قیمت",
    }.get(str(zone.get("kind") or ""), "ناحیه قیمتی")
    timeframe = str(zone.get("timeframe") or "نامشخص")
    status = str(zone.get("status") or "").lower()
    parts = [f"{kind} در تایم‌فریم {timeframe} شناسایی شده است"]
    if status == "fresh":
        parts.append("پس از تأیید هنوز برخورد مجزایی نداشته و دست‌نخورده است")
    elif status == "retested":
        parts.append("پس از تأیید دست‌کم یک بار بازآزمایی شده است")
    elif status == "filled":
        parts.append("ناحیه قبلاً پر شده است")
    count = zone.get("touch_count")
    if isinstance(count, int):
        parts.append(f"تعداد برخوردهای مجزا: {count}")
    return "؛ ".join(parts) + "."


def _forecast_lines(forecasts: list[dict[str, Any]], reason_limit: int) -> list[str]:
    names = {
        "support_bounce": "برگشت احتمالی از حمایت",
        "resistance_rejection": "واکنش احتمالی به مقاومت",
        "support_break": "شکست و بازآزمایی حمایت",
        "resistance_break": "شکست و بازآزمایی مقاومت",
        "trend_pullback": "اصلاح و ادامه روند",
    }
    status_names = {
        "fresh": "دست‌نخورده",
        "retested": "بازآزمایی‌شده",
        "filled": "پرشده",
        "invalidated": "بی‌اعتبارشده",
    }
    kind_names = {
        "support": "حمایت",
        "resistance": "مقاومت",
        "OB": "اوردر بلاک",
        "FVG": "ناحیه عدم تعادل قیمت",
    }
    lines: list[str] = []
    for index, item in enumerate(forecasts, 1):
        zone = item.get("level") or {}
        if not isinstance(zone, dict):
            continue
        scenario = names.get(str(item.get("scenario_type")), "سناریوی مشروط")
        direction = "لانگ" if item.get("direction") == "LONG" else "شورت"
        timeframe = str(zone.get("timeframe") or "نامشخص")
        low, high = zone.get("low"), zone.get("high")
        lines.append(f"{index}) {scenario} ({direction}) | {timeframe}")
        lines.append(f"ناحیه: {_price(low)} تا {_price(high)}")
        status = status_names.get(str(zone.get("status") or "").lower())
        if status:
            lines.append(f"وضعیت ناحیه: {status}")
        count = zone.get("touch_count")
        if isinstance(count, int):
            lines.append(f"تعداد برخوردهای مجزا: {count}")

        reason = safe_model_narrative_fa(item.get("reason_fa"), limit=reason_limit)
        if not reason:
            reason = _fallback_forecast_reason(zone)
        lines.append(f"شواهد: {reason}")

        condition = safe_model_narrative_fa(item.get("condition_fa"), limit=240)
        if not condition:
            condition = "ورود فقط پس از تأیید کندل بستهٔ 5m؛ لمس اولیه ناحیه مجوز ورود نیست."
        lines.append(f"شرط فعال‌شدن: {condition}")

        target = item.get("target")
        if isinstance(target, dict) and target.get("low") is not None and target.get("high") is not None:
            lines.append(
                f"مقصد ساختاری مشروط: {_price(target.get('low'))} تا "
                f"{_price(target.get('high'))} ({target.get('timeframe') or '—'})"
            )
        else:
            lines.append("مقصد ساختاری معتبر در داده‌های انتخاب‌شده مشخص نشده است.")

        invalidation = safe_model_narrative_fa(item.get("invalidation_fa"), limit=220)
        if not invalidation:
            invalidation = "تثبیت قیمت در سمت مخالف ناحیه، سناریو را بی‌اعتبار می‌کند."
        lines.append(f"ابطال: {invalidation}")
    return lines


_REJECTION_FA = {
    "continuation_trend_conflict": "پیشنهاد ادامه‌روند با ساختار تأییدشدهٔ 1H/4H هم‌جهت نبود.",
    "stop_too_wide": "حدضرر ساختاری از محدودهٔ مجاز موتور بیشتر بود.",
    "insufficient_net_reward_before_obstacle": "تا مانع ساختاری بعدی، بازده خالص کافی وجود نداشت.",
    "already_invalidated": "سطح ابطال پیش از پذیرش پلن شکسته شده بود.",
    "target_already_reached": "هدف پیش از ثبت پلن لمس شده بود.",
    "proposal_expired": "اعتبار زمانی پیشنهاد پایان یافته بود.",
    "missing_reference_swing": "پیوت مرجع لازم برای تأیید ساختار موجود نبود.",
    "unknown_evidence_id": "یکی از نواحی انتخاب‌شده در فهرست شواهد معتبر نبود.",
}


def _rejection_lines(summary: dict[str, Any]) -> list[str]:
    raw = summary.get("rejections")
    if not isinstance(raw, list) or not raw:
        return []
    lines = ["علت‌های رد پلن اجرایی:"]
    for item in raw[:3]:
        code = str(item)
        lines.append("- " + _REJECTION_FA.get(code, "شرط ساختاری یا اجرایی لازم تأیید نشد."))
    return lines


def build_ai_telegram_caption(
    *,
    symbol: str,
    live: float,
    decision: str | None,
    structured: dict[str, Any],
    validation: dict[str, Any],
    analyzed_at: datetime,
) -> str:
    """Short photo caption; the complete outlook is delivered in a following text message."""
    lines, summary, forecasts = _header_lines(
        symbol=symbol, live=live, analyzed_at=analyzed_at,
        structured=structured, validation=validation,
    )
    s = structured
    final = str(s.get("final_ai_decision") or decision or "NO_TRADE").upper()
    conditional = s.get("setup_mode") == "conditional" or final == "WAIT"
    if conditional or final in ("LONG", "SHORT"):
        lo = s.get("conditional_entry_zone_low") or s.get("entry_zone_low")
        hi = s.get("conditional_entry_zone_high") or s.get("entry_zone_high")
        lines.extend([
            f"ناحیه ورود: {_price(lo)} تا {_price(hi)}",
            f"ورود پیشنهادی: {_price(s.get('entry_price'))}",
            f"حدضرر: {_price(s.get('stop_loss'))} | هدف تحلیلی: {_price(s.get('take_profit'))}",
            f"سقف R:R اجرایی: 1:{EXECUTION_MAX_REWARD_R_MULTIPLE:g} قبل از هزینه‌ها",
        ])
    lines.append(
        "جزئیات تحلیل و سناریوهای مشروط در پیام متنی بعدی آمده است."
        if forecasts else "گزارش تحلیلی کامل در پیام متنی بعدی آمده است."
    )
    result = format_persian_report_for_telegram("\n".join(lines))
    if len(result.encode("utf-16-le")) // 2 <= 1024:
        return result
    compact = format_persian_report_for_telegram("\n".join(lines[:4]))
    if len(compact.encode("utf-16-le")) // 2 <= 1024:
        return compact
    return format_persian_report_for_telegram(_short(symbol, 20) + "\nگزارش کامل در پیام بعدی.")


def build_ai_telegram_summary(
    *,
    symbol: str,
    live: float,
    decision: str | None,
    structured: dict[str, Any],
    validation: dict[str, Any],
    report_fa: str,
    analyzed_at: datetime,
) -> str:
    """Full Telegram analysis, including watch-only outlook even when no plan is executable."""
    s = structured
    header, summary, forecasts = _header_lines(
        symbol=symbol, live=live, analyzed_at=analyzed_at,
        structured=s, validation=validation,
    )
    final = str(s.get("final_ai_decision") or decision or "NO_TRADE").upper()
    conditional = s.get("setup_mode") == "conditional" or final == "WAIT"
    rejected = final in ("LONG", "SHORT") and not validation.get("ok")

    for market_limit in (600, 400, 200, 0):
        for comparison_limit in (300, 160, 0):
            for reason_limit in (180, 100, 40, 0):
                sections = ["\n".join(header)]
                market_text = safe_model_narrative_fa(
                    summary.get("market_summary_fa"), limit=market_limit
                )
                if not market_text:
                    artifacts = s.get("_artifacts") or {}
                    features = artifacts.get("strategy_features") if isinstance(artifacts, dict) else {}
                    market_text = format_market_features_fa(
                        features if isinstance(features, dict) else {},
                        summary.get("execution_regime") if isinstance(summary.get("execution_regime"), dict) else {},
                    )
                if market_text:
                    sections.append("جمع‌بندی بازار:\n" + market_text)

                comparison = safe_model_narrative_fa(
                    summary.get("comparison_fa"), limit=comparison_limit
                )
                if comparison:
                    sections.append("مقایسه سناریوها:\n" + comparison)

                if forecasts:
                    sections.append(
                        "چشم‌انداز چهار‌ساعته ـ سناریوهای مشروط:\n" +
                        "\n".join(_forecast_lines(forecasts, reason_limit))
                    )
                    sections.append(
                        "این موارد فقط سناریوهای تحت نظر هستند؛ هیچ‌کدام به‌تنهایی سیگنال ورود یا سفارش Paper نیستند."
                    )
                elif summary and summary.get("accepted") == 0:
                    sections.extend(_rejection_lines(summary))
                    sections.append("در این نوبت پلن اجرایی معتبری پذیرفته نشده است.")

                levels: list[str] = []
                if conditional or final in ("LONG", "SHORT"):
                    lo = s.get("conditional_entry_zone_low") or s.get("entry_zone_low")
                    hi = s.get("conditional_entry_zone_high") or s.get("entry_zone_high")
                    levels = [
                        f"ناحیه ورود: {_price(lo)} تا {_price(hi)}",
                        f"ورود پیشنهادی: {_price(s.get('entry_price'))}",
                        f"حدضرر: {_price(s.get('stop_loss'))} | هدف تحلیلی: {_price(s.get('take_profit'))}",
                        f"سقف R:R اجرایی: 1:{EXECUTION_MAX_REWARD_R_MULTIPLE:g} قبل از هزینه‌ها",
                    ]
                if levels:
                    sections.append("\n".join(levels))

                if conditional:
                    condition = safe_model_narrative_fa(
                        s.get("confirmation_required") or s.get("entry_confirmation") or s.get("waiting_for"),
                        limit=reason_limit,
                    )
                    invalidation = safe_model_narrative_fa(
                        s.get("conditional_invalidation") or s.get("invalidation_condition"),
                        limit=reason_limit,
                    )
                    if condition:
                        sections.append("شرط ورود:\n" + condition)
                    if invalidation:
                        sections.append("ابطال:\n" + invalidation)
                elif not summary:
                    reason = (
                        "، ".join(map(str, validation.get("reasons") or []))
                        if rejected else
                        s.get("ict_structure") or s.get("trade_reason") or s.get("market_bias") or report_fa
                    )
                    reason = safe_model_narrative_fa(reason, limit=reason_limit)
                    if reason:
                        sections.append("دلیل:\n" + reason)

                sections.append("معامله آزمایشی؛ ورود فقط پس از تأییدهای مستقل موتور مجاز است.")
                result = format_persian_report_for_telegram("\n\n".join(x for x in sections if x))
                if len(result.encode("utf-16-le")) // 2 <= 4096:
                    return result

    # Last-resort bounded message: never silently collapse an outlook into "no active setup".
    fallback = format_persian_report_for_telegram(
        "\n".join(header + ["چشم‌انداز تفصیلی در پنل ثبت شده است.", "معامله آزمایشی؛ بدون تأیید ورود انجام نمی‌شود."])
    )
    return fallback
