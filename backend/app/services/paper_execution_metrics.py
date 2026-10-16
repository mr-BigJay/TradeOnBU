"""KPIs for paper execution quality (executed vs monitored candidates)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.setup_lifecycle import PENDING_ENTRY, POSITION_CLOSED, POSITION_OPEN, READY_LONG, READY_SHORT
from app.domain.tehran_time import format_tehran, report_period_bounds_utc
from app.models import AiProposedSetup, SimPosition
from app.services.paper_execution_budget import count_auto_paper_entry_orders_tehran_today


def _auto_paper(extra: dict | None) -> bool:
    return bool((extra or {}).get("auto_paper"))


async def build_execution_metrics(
    session: AsyncSession,
    *,
    period: str = "30d",
) -> dict[str, Any]:
    start, end = report_period_bounds_utc(period)
    now = datetime.now(timezone.utc)

    setup_q = select(AiProposedSetup)
    pos_q = select(SimPosition)
    if start and end:
        setup_q = setup_q.where(AiProposedSetup.created_at >= start, AiProposedSetup.created_at <= end)
        pos_q = pos_q.where(SimPosition.created_at >= start, SimPosition.created_at <= end)

    setups = list((await session.execute(setup_q.order_by(desc(AiProposedSetup.created_at)).limit(5000))).scalars())
    positions = list((await session.execute(pos_q)).scalars())

    ready_candidates = [
        s
        for s in setups
        if (s.lifecycle_state or "") in (READY_LONG, READY_SHORT, PENDING_ENTRY, POSITION_OPEN, POSITION_CLOSED)
        or (s.last_validation_result or {}).get("ready_for_paper")
        or (s.last_validation_result or {}).get("ok")
    ]
    auto_positions = [p for p in positions if _auto_paper(p.extra)]
    filled_auto = [p for p in auto_positions if p.fill_price is not None and p.status in ("open", "tp_hit", "sl_hit", "closed")]
    closed_auto = [p for p in auto_positions if p.status in ("closed", "tp_hit", "sl_hit")]

    wins = [p for p in closed_auto if (p.net_pnl_usdt or 0) > 0]
    losses = [p for p in closed_auto if (p.net_pnl_usdt or 0) < 0]
    gross_profit = sum(float(p.net_pnl_usdt or 0) for p in wins)
    gross_loss = abs(sum(float(p.net_pnl_usdt or 0) for p in losses))

    skip_reasons: dict[str, int] = {}
    for s in setups:
        vr = s.last_validation_result or {}
        reason = vr.get("order_skipped_reason") or s.last_rejection_reason
        if reason:
            skip_reasons[str(reason)] = skip_reasons.get(str(reason), 0) + 1

    candidate_to_fill = (
        len(filled_auto) / len(ready_candidates) if ready_candidates else None
    )
    win_rate = len(wins) / len(closed_auto) if closed_auto else None
    profit_factor = (gross_profit / gross_loss) if gross_loss > 1e-9 else None
    expectancy = (
        sum(float(p.net_pnl_usdt or 0) for p in closed_auto) / len(closed_auto) if closed_auto else None
    )

    tehran_today_orders = await count_auto_paper_entry_orders_tehran_today(session)

    return {
        "period": period,
        "bounds_utc": {"start": start.isoformat() if start else None, "end": end.isoformat() if end else None},
        "generated_at_tehran": format_tehran(now),
        "monitoring": {
            "setups_in_sample": len(setups),
            "ready_or_beyond_candidates": len(ready_candidates),
            "skip_reason_counts": skip_reasons,
        },
        "executed_auto_paper": {
            "orders_created": len(auto_positions),
            "filled": len(filled_auto),
            "pending_unfilled": len([p for p in auto_positions if p.status == "pending_entry"]),
            "open": len([p for p in auto_positions if p.status == "open"]),
            "closed": len(closed_auto),
            "market_orders": len([p for p in auto_positions if (p.order_type or "").lower() == "market"]),
            "limit_orders": len([p for p in auto_positions if (p.order_type or "").lower() == "limit"]),
            "candidate_to_fill_rate": candidate_to_fill,
            "win_rate_closed": win_rate,
            "profit_factor_closed": profit_factor,
            "expectancy_usdt_closed": expectancy,
            "tehran_today_auto_orders": tehran_today_orders,
        },
        "data_quality_note": (
            "Rates are observational on stored DB history; thin samples yield high uncertainty."
            if len(auto_positions) < 10
            else None
        ),
    }
