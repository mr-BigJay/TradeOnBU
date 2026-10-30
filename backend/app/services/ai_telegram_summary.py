"""Presentation-only summary: full analysis and trading decisions stay unchanged."""
from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any

from app.domain.execution_tp_cap import EXECUTION_MAX_REWARD_R_MULTIPLE
from app.domain.tehran_time import format_tehran
from app.services.telegram_message_format import format_persian_report_for_telegram


def _short(value: Any, limit: int) -> str:
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


def build_ai_telegram_summary(*, symbol: str, live: float, decision: str | None,
                              structured: dict[str, Any], validation: dict[str, Any],
                              report_fa: str, analyzed_at: datetime) -> str:
    s = structured
    final = str(s.get("final_ai_decision") or decision or "NO_TRADE").upper()
    conditional = s.get("setup_mode") == "conditional" or final == "WAIT"
    direction = str(s.get("potential_direction") if conditional else final).upper()
    direction = {"LONG": "لانگ", "SHORT": "شورت"}.get(direction, "نامشخص")
    rejected = final in ("LONG", "SHORT") and not validation.get("ok")
    status = ("رد اعتبارسنجی؛ ورود مجاز نیست" if rejected else
              f"انتظار برای {direction}؛ ورود تأیید نشده" if conditional else
              f"پیشنهاد {direction}؛ اجرای ورود مشروط به کنترل‌های سامانه" if final in ("LONG", "SHORT") else
              "عدم ورود؛ بدون سناریوی فعال")
    strategy = {"ict_reversal": "A · برگشت قیمت", "trend_continuation": "B · ادامه روند",
                "breakout_retest": "C · شکست و بازآزمایی"}.get(s.get("strategy_id"), "ICT")
    fixed = [f"{_short(symbol, 20)} | {strategy}", status,
             f"قیمت: {_price(live)} | زمان تهران: {format_tehran(analyzed_at)}"]
    regime = {"bearish": "نزولی", "bullish": "صعودی", "ranging": "رنج", "unclear": "نامشخص"}.get(s.get("market_regime"), "نامشخص")
    fixed.append(f"روند غالب: {regime}" + ("؛ سناریو خلاف روند" if s.get("trade_against_dominant_trend") else ""))
    levels = []
    if conditional or final in ("LONG", "SHORT"):
        lo = s.get("conditional_entry_zone_low") or s.get("entry_zone_low")
        hi = s.get("conditional_entry_zone_high") or s.get("entry_zone_high")
        levels = [f"ناحیه ورود: {_price(lo)} تا {_price(hi)}", f"ورود پیشنهادی: {_price(s.get('entry_price'))}",
                  f"حدضرر: {_price(s.get('stop_loss'))} | هدف تحلیلی: {_price(s.get('take_profit'))}",
                  f"سقف R:R اجرایی: 1:{EXECUTION_MAX_REWARD_R_MULTIPLE:g} قبل از هزینه‌ها"]
    reason = ("، ".join(map(str, validation.get("reasons") or [])) if rejected else
              s.get("ict_structure") or s.get("trade_reason") or s.get("market_bias") or report_fa)
    condition = s.get("confirmation_required") or s.get("entry_confirmation") or s.get("waiting_for")
    invalidation = s.get("conditional_invalidation") or s.get("invalidation_condition")
    # Budget the formatted caption, including BiDi isolates and non-BMP characters.
    for budget in (210, 170, 130, 90, 60, 30, 0):
        narrative = [f"دلیل: {_short(reason, budget)}"] if budget and reason else []
        if condition and budget:
            narrative.append(f"شرط ورود: {_short(condition, budget)}")
        if invalidation and budget:
            narrative.append(f"ابطال: {_short(invalidation, budget)}")
        result = format_persian_report_for_telegram("\n\n".join([
            "\n".join(fixed), "\n".join(narrative), "\n".join(levels), "معامله آزمایشی؛ گزارش کامل در پنل."
        ]))
        if len(result.encode("utf-16-le")) // 2 <= 1024:
            return result
    raise ValueError("Telegram summary fixed fields exceed caption limit")
