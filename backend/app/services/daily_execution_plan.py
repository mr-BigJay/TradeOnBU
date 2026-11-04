"""Read-only snapshot for the daily report's intraday execution card."""
from datetime import datetime, timedelta
import math

from sqlalchemy import select

from app.models import AiProposedSetup, SimPositionSettings
from app.services.conditional_revision import objective_confirmations_satisfied_for_order
from app.services.entry_strategies import A, strategy_id, strategy_settings
from app.services.execution_model_sim import auto_paper_rr_from_settings


def wait_plan(reason):
    return dict(source="ict_engine", action="wait", rationale_fa=reason,
                trigger_fa="پس از تأیید ستاپ برگشتی و وجود فضای کافی تا اولین مانع، دوباره بررسی شود.",
                entry_level_id=None, stop_level_id=None, target_level_ids=[])


def target_at_two_r(sim, direction, entry, stop, obstacle):
    """First BTCUSDT price tick reaching net 2R before or at the obstacle."""
    sign = 1 if direction == "LONG" else -1
    first = math.floor(entry * 10) + 1 if sign == 1 else -math.ceil(entry * 10) + 1
    last = math.floor(sign * obstacle * 10)

    def acceptable(tick):
        return auto_paper_rr_from_settings(sim, direction=direction, entry=entry,
            sl=stop, tp=sign * tick / 10, min_net_rr=2).acceptable

    if first > last or not acceptable(last):
        return None
    while first < last:
        middle = (first + last) // 2
        if acceptable(middle):
            last = middle
        else:
            first = middle + 1
    return sign * first / 10


async def snapshot_plan(session, symbol, context):
    waiting = wait_plan("در زمان گزارش، ستاپ تأییدشده و قابل استفاده برای استراتژی A یافت نشد.")
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
        if not objective_confirmations_satisfied_for_order(row)[0]:
            continue
        conf = row.confirmation_state or {}
        completed = set(conf.get("completed") or [])
        if not {"liquidity_sweep", "entry_zone_touch"} <= completed or not completed.intersection(("mss", "bos")):
            continue
        sign = 1 if row.direction == "LONG" else -1
        entry, stop, inv, target = row.entry_price, row.stop_loss, row.invalidation_level, row.take_profit
        if not all(v and math.isfinite(v) and v > 0 for v in (entry, stop, inv, target)):
            continue
        # The stop must cover the engine's structural invalidation level.
        if sign * (entry - inv) <= 0 or sign * (inv - stop) < 0 or sign * (target - entry) <= 0:
            continue
        levels = [float(level["price"]) for level in context["levels"]]
        obstacles = [target] + [v for v in levels if sign * (v - entry) > 0]
        obstacle = min(obstacles) if sign == 1 else max(obstacles)
        tp = target_at_two_r(sim, row.direction, entry, stop, obstacle)
        if tp is None:
            waiting = wait_plan("تا اولین سطح ساختاری، پس از هزینه‌های اجرای Paper فضای کافی برای ۲R وجود ندارد.")
            continue
        quote = context.get("live_price")
        if quote is None or sign * (quote - stop) <= 0 or sign * (tp - quote) <= 0:
            continue
        rr = auto_paper_rr_from_settings(sim, direction=row.direction, entry=entry,
                                         sl=stop, tp=tp, min_net_rr=2)
        return dict(source="ict_engine", setup_id=row.setup_id, action=row.direction.lower(),
                    rationale_fa="موتور برگشتی A جمع‌آوری نقدینگی، شکست ساختار و لمس محدوده را ثبت کرده است.",
                    trigger_fa="این سطوح مربوط به وضعیت ستاپ هنگام تهیهٔ گزارش هستند.",
                    entry_price=entry, stop_loss=stop, take_profits=[tp], risk_reward=[rr.net_rr])
    return waiting
