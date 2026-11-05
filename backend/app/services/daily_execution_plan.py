"""Read-only snapshot for the daily report's intraday execution card."""
from datetime import datetime, timedelta


from sqlalchemy import select

from app.models import AiProposedSetup, SimPosition, SimPositionSettings
from app.services.ict_execution_plan import REASONS, target_at_two_r
from app.services.entry_strategies import A, strategy_id, strategy_settings
from app.services.execution_model_sim import auto_paper_rr_from_settings


def wait_plan(reason):
    return dict(source="ict_engine", action="wait", rationale_fa=reason,
                trigger_fa="پس از تأیید ستاپ برگشتی و وجود فضای کافی تا اولین مانع، دوباره بررسی شود.",
                entry_level_id=None, stop_level_id=None, target_level_ids=[])


async def snapshot_plan(session, symbol, context):
    waiting = wait_plan("در زمان گزارش، جمع‌آوری نقدینگی، شکست ساختار و ابطال معتبر برای ورود A تأیید نشده‌اند.")
    if not (await strategy_settings(session))[A]:
        return wait_plan("استراتژی برگشتی A غیرفعال است.")
    stamp = datetime.fromisoformat(context["as_of"].replace("Z", "+00:00"))
    rows = (await session.scalars(select(AiProposedSetup).where(
        AiProposedSetup.symbol == symbol, AiProposedSetup.status == "open",
        AiProposedSetup.lifecycle_state.in_(("READY_LONG", "READY_SHORT", "PENDING_ENTRY")),
        AiProposedSetup.updated_at <= stamp,
        AiProposedSetup.last_monitor_at.between(stamp - timedelta(minutes=5), stamp),
        AiProposedSetup.expires_at > stamp,
    ).order_by(AiProposedSetup.last_monitor_at.desc(), AiProposedSetup.id.desc()))).all()
    sim = await session.get(SimPositionSettings, 1) if rows else None
    for row in rows:
        if strategy_id(row.structured_snapshot or {}) != A or sim is None:
            continue
        notes = row.objective_notes or {}
        health = notes.get("market_data_health") or {}
        validation = row.last_validation_result or {}
        reason = validation.get("order_skipped_reason") or validation.get("reason")
        if health.get("ok") is not True or notes.get("pending_fill_suspended") or notes.get("pending_target_review_required"):
            waiting = wait_plan(REASONS["ict_structure_data_gap"])
            continue
        if row.lifecycle_state != "PENDING_ENTRY" or validation.get("ok") is False:
            waiting = wait_plan(REASONS.get(reason, "تأیید نهایی موتور و ثبت سفارش در انتظار است." if not reason else f"موتور ورود سفارش را متوقف کرده است ({reason})."))
            continue
        pos = await session.scalar(select(SimPosition).where(
            SimPosition.ai_setup_id == row.setup_id, SimPosition.symbol == symbol,
            SimPosition.status == "pending_entry", SimPosition.updated_at <= stamp,
        ).order_by(SimPosition.id.desc()).limit(1))
        if pos is None or not ((pos.extra or {}).get("strict_a_plan") or (pos.extra or {}).get("verified_trade_plan")):
            continue
        entry, stop, tp = pos.limit_entry_price, pos.stop_loss, pos.take_profit
        sign = 1 if pos.direction == "LONG" else -1
        quote = context.get("live_price")
        if not all((entry, stop, tp, quote)) or sign * (quote - stop) <= 0 or sign * (tp - quote) <= 0:
            continue
        rr = auto_paper_rr_from_settings(sim, direction=pos.direction, entry=entry, sl=stop, tp=tp, min_net_rr=2)
        if not rr.acceptable:
            continue
        return dict(source="ict_engine", setup_id=row.setup_id, action=pos.direction.lower(),
                    rationale_fa="تأییدهای برگشتی تکمیل شده و سفارش Paper ثبت شده است.",
                    trigger_fa="قیمت‌ها از سفارش در انتظار پرشدن در زمان گزارش خوانده شده‌اند.",
                    entry_price=entry, stop_loss=stop, take_profits=[tp], risk_reward=[rr.net_rr])
    return waiting
