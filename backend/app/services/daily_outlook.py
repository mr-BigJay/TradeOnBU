"""Independent outlook per closed 4h slot (or legacy day); pre-AI manual retries only."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.config import get_effective_settings
from app.db import SessionLocal
from app.models import DailyOutlookSettings, DailyOutlookRun
from app.services.ai_analysis_scheduler import acquire_analysis_lock, release_analysis_lock
from app.services.ai_api_reasoning import effective_luna_api_effort
from app.services.ai_market_intelligence import load_ai_market_intelligence
from app.services.daily_execution_plan import snapshot_plan
from app.services.daily_outlook_data import build_daily_context
from app.services.daily_outlook_schema import INSTRUCTIONS, parse_outlook
from app.services.settings_store import get_plain, get_secret
from app.services.siwc.credentials import get_access_token
from app.services.siwc.model_profiles import resolve_model_selection
from app.services.siwc.responses_client import stream_analysis_response, OpenAIResponsesError

TEHRAN = ZoneInfo("Asia/Tehran")
logger = logging.getLogger(__name__)
RUN_TIMEOUT = 900
PRE_AI_ERRORS = {
    "insufficient_market_history", "latest_closed_candle_missing", "invalid_market_prices",
    "invalid_candle_range", "market_history_gap", "incomplete_4h_history",
    "insufficient_weekly_history", "stale_live_quote", "openai_api_key_missing",
    "ai_connection_unavailable",
}


def retryable(row) -> bool:
    # Also recognizes pre-upgrade validation failures. Unknown/paid failures fail closed.
    return bool(row and row.status == "failed" and row.error_code in PRE_AI_ERRORS
                and not row.model_slug and not row.usage
                and not (row.context or {}).get("ai_request_started"))


def utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def scheduled_time(now: datetime, hour: int, minute: int) -> datetime:
    return utc(now).astimezone(TEHRAN).replace(hour=hour, minute=minute, second=0, microsecond=0)


def due(now: datetime, hour: int, minute: int) -> bool:
    return utc(now) >= scheduled_time(now, hour, minute)


def four_hour_enabled() -> bool:
    return os.getenv("OUTLOOK_FOUR_HOUR_ENABLED", "0") == "1"


def four_hour_slot(now):
    now = utc(now).astimezone(timezone.utc)
    close = now.replace(hour=(now.hour//4)*4, minute=0, second=0, microsecond=0)
    return close, close + timedelta(minutes=5)


async def settings_row(session) -> DailyOutlookSettings:
    row = await session.get(DailyOutlookSettings, 1)
    if row is None:
        row = DailyOutlookSettings(id=1, enabled=False, hour=8, minute=5)
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
        row = await session.get(DailyOutlookSettings, 1)
    if four_hour_enabled() and not row.four_hour_initialized:
        row.enabled, row.four_hour_initialized = True, True
        await session.commit()
    return row


async def provider_credentials() -> tuple[str, str, str]:
    settings = get_effective_settings()
    async with SessionLocal() as session:
        provider = await get_plain(session, "ai", "provider", settings.ai_provider)
        if provider == "openai_api":
            token = await get_secret(session, "ai", "openai_api_key") or settings.openai_api_key.strip()
            if not token:
                raise ValueError("openai_api_key_missing")
            effort = await get_plain(session, "ai", "api_reasoning_effort", "high")
            return token, "gpt-6-luna", effective_luna_api_effort(effort)
        token, cred, err = await get_access_token(session)
        await session.commit()
        if err or not token:
            raise ValueError("ai_connection_unavailable")
        model, effort = resolve_model_selection(
            cred.preferred_model if cred else None,
            settings.ai_analysis_model, settings.ai_reasoning_effort,
        )
        return token, model, effort


async def recover_interrupted(session, now: datetime) -> None:
    # Longer than both the total work deadline and shared lock lease. No auto retry.
    await session.execute(update(DailyOutlookRun).where(
        DailyOutlookRun.status == "running",
        DailyOutlookRun.started_at < now - timedelta(minutes=32),
    ).values(status="failed", error_code="interrupted_run", completed_at=now))
    await session.commit()


async def reserve_run(trigger: str) -> tuple[int | None, str | None, str]:
    now = datetime.now(timezone.utc)
    symbol = get_effective_settings().default_symbol
    date = now.astimezone(TEHRAN).date().isoformat()
    async with SessionLocal() as session:
        await recover_interrupted(session, now)
        cfg = await settings_row(session)
        slot, ready = four_hour_slot(now)
        slot_ms = int(slot.timestamp()*1000) if four_hour_enabled() else 0
        if slot_ms:
            date = slot.astimezone(TEHRAN).date().isoformat()
        if four_hour_enabled() and now < ready:
            return None, None, "waiting_for_closed_4h"
        if trigger == "scheduled" and (not cfg.enabled or
                (four_hour_enabled() and now >= ready + timedelta(minutes=15)) or
                (not four_hour_enabled() and not due(now, cfg.hour, cfg.minute))):
            return None, None, "not_due"
        existing = (await session.execute(select(DailyOutlookRun).where(
            DailyOutlookRun.symbol == symbol, DailyOutlookRun.report_date == date,
            DailyOutlookRun.slot_close_ms == slot_ms,
        ))).scalar_one_or_none()
        if existing and not (trigger == "manual" and retryable(existing)):
            return existing.id, None, "already_attempted_today"
    owner = "daily-" + uuid.uuid4().hex
    # Share the existing lock; hourly/manual analyses cannot overlap daily inference.
    if not await acquire_analysis_lock(owner, ttl_seconds=1800):
        return None, None, "analysis_busy"
    try:
        async with SessionLocal() as session:
            # Recheck after acquiring the shared lock: two clicks must not retry twice.
            row = (await session.execute(select(DailyOutlookRun).where(
                DailyOutlookRun.symbol == symbol, DailyOutlookRun.report_date == date,
                DailyOutlookRun.slot_close_ms == slot_ms,
            ))).scalar_one_or_none()
            if row:
                if trigger != "manual" or not retryable(row):
                    await release_analysis_lock(owner)
                    return row.id, None, "already_attempted_today"
                history = list((row.context or {}).get("pre_ai_failures", []))
                history.append({"error_code": row.error_code, "started_at": utc(row.started_at).isoformat()})
                row.context = {"pre_ai_failures": history[-20:]}
                row.trigger, row.status, row.started_at = trigger, "running", now
                row.completed_at, row.error_code = None, None
                row.report, row.usage, row.model_slug = {}, {}, None
            else:
                row = DailyOutlookRun(symbol=symbol, report_date=date, slot_close_ms=slot_ms, trigger=trigger,
                                      started_at=now, status="running")
                session.add(row)
            await session.commit()
            return row.id, owner, "started"
    except IntegrityError:
        await release_analysis_lock(owner)
        return None, None, "already_attempted_today"
    except BaseException:
        await release_analysis_lock(owner)
        raise


async def _work(run_id: int) -> None:
    async with SessionLocal() as session:
        row = await session.get(DailyOutlookRun, run_id)
        symbol = row.symbol
        slot_ms = row.slot_close_ms
    # Data acquisition/validation precedes the only paid inference call.
    context = await build_daily_context(symbol)
    if slot_ms:
        bars = (context.get("candles") or {}).get("4h") or []
        if not bars or bars[-1][0] + 14_400_000 != slot_ms:
            raise ValueError("latest_closed_candle_missing")
        context["report_cadence"] = "4h"
        context["slot_close_ms"] = slot_ms
    async with SessionLocal() as session:
        execution_plan = await snapshot_plan(session, symbol, context)
        context["market_intelligence"] = await load_ai_market_intelligence(session, symbol=symbol)
        previous = (await session.execute(select(DailyOutlookRun).where(
            DailyOutlookRun.symbol == symbol, DailyOutlookRun.status == "completed",
            DailyOutlookRun.id < run_id,
        ).order_by(DailyOutlookRun.id.desc()).limit(1))).scalar_one_or_none()
        prior = {"date": previous.report_date, "summary": previous.report.get("summary_fa"),
                 "dominant": previous.report.get("dominant")} if previous else None
    token, model, effort = await provider_credentials()
    async with SessionLocal() as session:
        row = await session.get(DailyOutlookRun, run_id)
        row.context = {**context, "ai_request_started": True,
                       "pre_ai_failures": (row.context or {}).get("pre_ai_failures", [])}
        row.model_slug = model
        await session.commit()
    response = await stream_analysis_response(
        token, model=model, instructions=INSTRUCTIONS + ("\nThis is the four-hour refresh after a closed 4h candle. Keep the multi-day context but clearly explain changes since the previous report." if slot_ms else ""),
        user_input=json.dumps({"market": context, "previous_report": prior}, ensure_ascii=False),
        reasoning_effort=effort, timeout=600, max_output_tokens=12000,
    )
    # Store usage even when a syntactically valid completion fails schema validation.
    async with SessionLocal() as session:
        row = await session.get(DailyOutlookRun, run_id)
        row.usage = response.response_metadata.get("usage") or {}
        await session.commit()
    report = parse_outlook(response.text, context, execution_plan=execution_plan)
    async with SessionLocal() as session:
        row = await session.get(DailyOutlookRun, run_id)
        row.report = report
        row.status = "completed"
        row.completed_at = datetime.now(timezone.utc)
        await session.commit()
    if slot_ms:
        try:
            await deliver_outlook(run_id)
        except Exception:
            logger.exception("Outlook %s completed but Telegram delivery failed", run_id)


async def deliver_outlook(run_id):
    from app.services.telegram_notifier import TelegramNotifier
    async with SessionLocal() as session:
        row = await session.get(DailyOutlookRun, run_id)
        if row is None or row.status != "completed" or (row.context or {}).get("telegram_delivery"):
            return
        notifier = TelegramNotifier()
        if not notifier.enabled:
            row.context = {**row.context, "telegram_delivery": {"status": "disabled"}}
            await session.commit()
            return
        # Record intent before sending; ambiguous network outcomes must not resend AI/reports.
        row.context = {**row.context, "telegram_delivery": {"status": "sending"}}
        await session.commit()
        report = row.report or {}
        close = datetime.fromtimestamp(row.slot_close_ms/1000, timezone.utc).astimezone(TEHRAN)
        lines = [f"تحلیل چهار‌ساعته {row.symbol} | {close:%Y-%m-%d %H:%M} تهران",
                 report.get("summary_fa", "")]
        for key, title in (("dominant", "سناریوی اصلی"), ("alternative", "سناریوی جایگزین")):
            sc = report.get(key) or {}
            lines += [title + ": " + sc.get("title_fa", ""),
                      "شرط فعال‌شدن: " + sc.get("activation_fa", ""),
                      "شرط ابطال: " + sc.get("invalidation_fa", ""),
                      *sc.get("path_fa", [])]
        lines += ["ابهام‌ها: " + report.get("uncertainty_fa", ""),
                  "این گزارش به‌تنهایی سفارش ایجاد نمی‌کند؛ اجرا در حالت Paper است."]
        public = (notifier.settings.dashboard_public_url or "").strip()
        if public.startswith("https://"):
            lines.append(public.rstrip('/') + f"/#/outlook/reports/{row.id}")
        try:
            message_id = await notifier.send_message("\n\n".join(lines)[:4000], parse_mode=None)
            delivery = {"status": "sent" if message_id else "failed_or_unknown", "message_id": message_id}
        except Exception:
            delivery = {"status": "failed_or_unknown"}
        row.context = {**row.context, "telegram_delivery": delivery}
        await session.commit()


async def execute_reserved(run_id: int, owner: str) -> None:
    try:
        await asyncio.wait_for(_work(run_id), timeout=RUN_TIMEOUT)
    except asyncio.CancelledError:
        # A process interruption is recovered on the next daily tick; no duplicate call.
        raise
    except Exception as exc:
        if isinstance(exc, TimeoutError):
            code = "analysis_timeout"
        elif isinstance(exc, OpenAIResponsesError):
            code = "ai_request_failed"
        elif isinstance(exc, ValueError) and str(exc) in {
            "insufficient_market_history", "latest_closed_candle_missing",
            "invalid_market_prices", "invalid_candle_range", "market_history_gap",
            "incomplete_4h_history", "insufficient_weekly_history", "stale_live_quote",
            "openai_api_key_missing", "ai_connection_unavailable",
        }:
            code = str(exc)
        else:
            code = "outlook_generation_failed"
        logger.warning("Daily outlook %s failed: %s (%s)", run_id, code, type(exc).__name__)
        async with SessionLocal() as session:
            row = await session.get(DailyOutlookRun, run_id)
            if row.status == "completed":
                # Delivery timeout must not turn a successfully saved analysis into failure.
                return
            row.status = "failed"
            row.error_code = code
            row.completed_at = datetime.now(timezone.utc)
            if isinstance(exc, OpenAIResponsesError) and exc.partial:
                row.usage = exc.partial.response_metadata.get("usage") or {}
            await session.commit()
    finally:
        await release_analysis_lock(owner)


async def daily_outlook_tick() -> None:
    run_id, owner, _ = await reserve_run("scheduled")
    if owner and run_id:
        await execute_reserved(run_id, owner)


def serialize_run(row: DailyOutlookRun, *, detail: bool = True) -> dict:
    out = {"id": row.id, "symbol": row.symbol, "report_date": row.report_date,
           "status": row.status, "trigger": row.trigger, "slot_close_ms": row.slot_close_ms,
           "started_at": utc(row.started_at).isoformat(),
           "completed_at": utc(row.completed_at).isoformat() if row.completed_at else None,
           "model": row.model_slug, "error_code": row.error_code,
           "stale": (row.slot_close_ms != int(four_hour_slot(datetime.now(timezone.utc))[0].timestamp()*1000) if row.slot_close_ms else row.report_date != datetime.now(TEHRAN).date().isoformat())}
    report = row.report or {}
    dominant = report.get("dominant") or {}
    out.update(title=dominant.get("title_fa") or "چشم‌انداز روزانه بیت‌کوین",
               summary=(report.get("summary_fa") or "")[:240],
               direction=dominant.get("direction"),
               snapshot_price=(row.context or {}).get("live_price"))
    if detail:
        ctx = row.context or {}
        out.update(report=row.report, usage=row.usage, context={
            "as_of": ctx.get("as_of"), "source": ctx.get("source"),
            "live_price": ctx.get("live_price"), "levels": ctx.get("levels", []),
            "daily_atr14": ctx.get("daily_atr14"),
            "channel_context": ctx.get("channel_context"),
            "telegram_delivery": ctx.get("telegram_delivery"),
            "data_quality_warnings": ctx.get("data_quality_warnings", []),
            "daily_candles": (ctx.get("candles") or {}).get("1d", [])[-30:],
            "four_hour_candles": (ctx.get("candles") or {}).get("4h", [])[-120:],
        })
    return out


async def dashboard() -> dict:
    now = datetime.now(timezone.utc)
    symbol = get_effective_settings().default_symbol
    async with SessionLocal() as session:
        await recover_interrupted(session, now)
        cfg = await settings_row(session)
        rows = (await session.execute(select(DailyOutlookRun).where(
            DailyOutlookRun.symbol == symbol,
        ).order_by(DailyOutlookRun.id.desc()).limit(31))).scalars().all()
        slot, ready = four_hour_slot(now)
        slot_ms = int(slot.timestamp()*1000) if four_hour_enabled() else 0
        today = next((r for r in rows if r.report_date == (slot if slot_ms else now).astimezone(TEHRAN).date().isoformat() and r.slot_close_ms == slot_ms), None)
        nxt = scheduled_time(now, cfg.hour, cfg.minute)
        if today:
            nxt += timedelta(days=1)
        elif nxt < now:
            nxt = now.astimezone(TEHRAN)
        if four_hour_enabled():
            nxt = ready if now < ready or (not today and now < ready + timedelta(minutes=15)) else ready + timedelta(hours=4)
        return {
            "settings": {"enabled": cfg.enabled, "hour": cfg.hour, "minute": cfg.minute,
                         "cadence_hours": 4 if four_hour_enabled() else 24,
                         "timezone": "Asia/Tehran", "next_run_at": nxt.isoformat() if cfg.enabled else None},
            "latest": serialize_run(rows[0]) if rows else None,
            "latest_completed": serialize_run(next(r for r in rows if r.status == "completed"))
                if any(r.status == "completed" for r in rows) else None,
            "history": [serialize_run(r, detail=False) for r in rows[:30]],
            "next_cursor": rows[29].id if len(rows) > 30 else None,
            "can_generate": (not four_hour_enabled() or now >= ready) and (today is None or retryable(today)),
            "attempted_today": today is not None,
            "can_retry_today": retryable(today),
        }
