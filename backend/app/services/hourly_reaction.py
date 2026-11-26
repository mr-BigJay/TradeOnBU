"""Review the existing BTC playbook at each UTC hourly close. Paper execution only."""
import json
import math
import os
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.config import get_effective_settings
from app.db import SessionLocal
from app.domain.trading_calendar import new_entries_allowed
from app.models import AiAnalysisRun, HourlyReaction, SimPosition
from app.services import multi_setup_analyst as analyst
from app.services.ai_analysis_scheduler import scheduling_allowed
from app.services.ai_market_payload import serialize_candle
from app.services.market_data import MarketDataService
from app.services.position.execution_adapters import OrderRequest
from app.services.position.position_service import SimPositionService
from app.services.position.settings_service import get_or_create_settings
from app.services.runtime_settings import get_runtime_settings
from app.services.telegram_message_format import format_persian_report_for_telegram
from app.services.telegram_notifier import TelegramNotifier

from app.services.hourly_playbook import LABELS, STRATEGIES, READ_PROMPT

HOUR = 3_600_000

def enabled():
    return analyst.enabled() and os.getenv("HOURLY_AI_REACTION_ENABLED", "1") == "1"


async def ask(session, payload):
    from app.services.ai_ict_analysis import (_extract_json, effective_luna_api_effort, get_access_token,
        get_plain, get_secret, resolve_model_selection, stream_analysis_response)
    settings = get_effective_settings()
    provider = await get_plain(session,"ai","provider",settings.ai_provider)
    if provider == "openai_api":
        token = await get_secret(session,"ai","openai_api_key") or settings.openai_api_key.strip()
        model = "gpt-6-luna"
        effort = effective_luna_api_effort(await get_plain(session,"ai","api_reasoning_effort","high"))
    else:
        token, cred, error = await get_access_token(session)
        if error or not token:
            raise RuntimeError("hourly_credentials_unavailable")
        model, effort = resolve_model_selection(cred.preferred_model if cred else None,
                                                settings.ai_analysis_model, settings.ai_reasoning_effort)
    await session.commit()
    response = await stream_analysis_response(token, model=model, instructions=READ_PROMPT,
        user_input=json.dumps(payload, ensure_ascii=False), reasoning_effort=effort,
        timeout=settings.ai_response_timeout_seconds, max_output_tokens=2500)
    return _extract_json(response.text)


async def notify_pending(now_ms):
    notifier = TelegramNotifier()
    if not notifier.enabled:
        return
    async with SessionLocal() as session:
        slot = await session.scalar(select(HourlyReaction.close_ms).where(
            HourlyReaction.notice.is_not(None), HourlyReaction.telegram_id.is_(None),
            HourlyReaction.notify_after_ms<=now_ms).order_by(HourlyReaction.close_ms).limit(1))
        if slot is None:
            return
        lease = await session.execute(update(HourlyReaction).where(HourlyReaction.close_ms==slot,
            HourlyReaction.notify_after_ms<=now_ms, HourlyReaction.telegram_id.is_(None)
            ).values(notify_after_ms=now_ms+300_000))
        await session.commit()
        if not lease.rowcount:
            return
        row = await session.get(HourlyReaction,slot)
        message_id = await notifier.send_ai_report(format_persian_report_for_telegram(row.notice))
        if message_id:
            row.telegram_id = message_id
            await session.commit()


async def review_close(slot, bars):
    from app.services.entry_strategies import strategy_settings
    boundary = datetime.fromtimestamp(slot/1000, timezone.utc)
    async with SessionLocal() as session:
        # Use only plans already published before the reviewed candle opened.
        run = await session.scalar(select(AiAnalysisRun).where(
            AiAnalysisRun.completed_at<=boundary-timedelta(hours=1), AiAnalysisRun.completed_at>=boundary-timedelta(hours=6),
            AiAnalysisRun.status.in_(("completed","completed_with_warnings")),
            AiAnalysisRun.structured["analyst_summary"]["hourly_watch"].as_string().is_not(None)
        ).order_by(AiAnalysisRun.completed_at.desc()).limit(1))
        summary = (run.structured or {})["analyst_summary"] if run else {}
        available = await strategy_settings(session)
        plans = {k:p for k,p in summary.get("hourly_watch",{}).items() if available.get(STRATEGIES[p["strategy"]])}
        rows = (await session.execute(select(HourlyReaction, SimPosition.status).outerjoin(
            SimPosition, SimPosition.public_id==HourlyReaction.position_id).where(
            HourlyReaction.executed_plan.in_(list(plans))))).all()
        outcomes = {row.executed_plan:status for row,status in rows}
        recent = (await session.scalars(select(SimPosition).where(
            SimPosition.extra["hourly_plan"].as_string().is_not(None)).order_by(SimPosition.id.desc()).limit(8))).all()
        history = [{"key":p.extra["hourly_plan"], "status":p.status, "entry":p.fill_price,
                    "stop":p.stop_loss,"target":p.take_profit,"close_reason":p.close_reason} for p in recent]
        previous = await session.scalar(select(HourlyReaction).where(HourlyReaction.close_ms<slot)
                                       .order_by(HourlyReaction.close_ms.desc()).limit(1))
        paper = await get_or_create_settings(session)
        payload = dict(hour_close_ms=slot, hourly_candles=[serialize_candle(b) for b in bars],
                       scenario=summary.get("market_scenario"), watch=plans, outcomes=outcomes, recent_positions=history,
                       previous_review=previous.detail if previous else None,
                       fees=dict(taker=paper.taker_fee_rate,slippage_bps=paper.slippage_bps))
        run_id = run.id if run else None
        stop_entries = (summary.get("market_scenario",{}).get("event") or {}).get("status") == "pause"
        decision = await ask(session,payload)
    async with SessionLocal() as session:
        row = await session.get(HourlyReaction,slot)
        row.run_id = run_id
        row.detail = decision
        action = decision.get("action")
        if action == "WAIT":
            row.status = "wait"
        elif action == "ENTER":
            key = decision.get("plan_key")
            if not isinstance(key,str) or key not in plans or key in outcomes:
                raise ValueError("unknown_or_consumed_plan")
            plan = plans[key]
            paper = await get_or_create_settings(session)
            runtime = await get_runtime_settings(session)
            if stop_entries:
                row.status = "scenario_paused"
            elif not (await strategy_settings(session)).get(STRATEGIES[plan["strategy"]]) or not paper.auto_open_paper_positions or not paper.initial_balance_set or runtime.paper_trading_paused or runtime.paper_entry_dry_run:
                row.status = "paper_disabled"
            elif int(datetime.now(timezone.utc).timestamp()*1000) >= slot+HOUR:
                row.status = "stale_decision"
            else:
                stop,target = float(decision["stop_loss"]),float(decision["take_profit"])
                if not all(math.isfinite(v) and v>0 for v in (stop,target)) or not decision.get("reason_fa"):
                    raise ValueError("invalid_order_fields")
                row.executed_plan = key
                await session.flush()
                service = SimPositionService()
                from app.services.verified_plan_risk import budget
                price, _ = await service.market.fetch_ticker("BTCUSDT")
                risk, reason = await budget(session, paper, SimpleNamespace(direction=plan["direction"],
                    entry_price=price,stop_loss=stop),datetime.now(timezone.utc))
                if not risk:
                    row.executed_plan = None
                    row.status = "wallet_limit"
                    row.detail = {**decision,"execution_reason":reason}
                    await session.commit()
                    return
                order = OrderRequest("BTCUSDT",plan["direction"],risk["margin"],risk["leverage"],
                    "market",None,stop,target,order_extra={"hourly_plan":key,"hour_close_ms":slot})
                result = await service.create_order(session,order,commit=False)
                if result.success and result.payload.get("status")=="open":
                    pos = await service.get_position_by_public_id(session,result.position_id)
                    row.status,row.position_id = "opened",pos.public_id
                    row.notice = (f"پوزیشن آزمایشی {pos.direction} بیت‌کوین باز شد\n"
                        f"استراتژی {plan['strategy']} · {LABELS[plan['strategy']]}\n"
                        f"ناحیه {plan['zone_low']:,.1f} تا {plan['zone_high']:,.1f}\n"
                        f"واکنش یک‌ساعته: {str(decision['reason_fa'])[:600]}\n"
                        f"ورود انجام‌شده {pos.fill_price:,.1f} | هدف {pos.take_profit:,.1f} | حد ضرر {pos.stop_loss:,.1f}")
                else:
                    row.status,row.executed_plan = "unfilled",None
        else:
            raise ValueError("invalid_hourly_action")
        await session.commit()


async def tick():
    if not enabled():
        return
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp()*1000)
    await notify_pending(now_ms)
    if not new_entries_allowed(now):
        return
    slot = now_ms//HOUR*HOUR
    async with SessionLocal() as session:
        if await session.get(HourlyReaction,slot) or not (await scheduling_allowed(session))[0]:
            return
    bars = await MarketDataService(get_effective_settings()).fetch_closed_klines("BTCUSDT","1h",12)
    bars = [b for b in bars if b.time_ms+HOUR<=slot]
    if not bars or bars[-1].time_ms+HOUR != slot:
        return
    async with SessionLocal() as session:
        session.add(HourlyReaction(close_ms=slot))
        try:
            await session.commit()
        except IntegrityError:
            return
    try:
        await review_close(slot,bars)
    except Exception as exc:
        async with SessionLocal() as session:
            row = await session.get(HourlyReaction,slot)
            row.status,row.detail = "failed",{"error":type(exc).__name__,"reason":str(exc)[:180]}
            await session.commit()
        raise
    await notify_pending(now_ms)
