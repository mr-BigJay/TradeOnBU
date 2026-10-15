from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AiAnalysisRun, IctSetupAuditEvent, SimPosition


async def log_setup_event(
    session: AsyncSession,
    *,
    setup_id: str,
    event_type: str,
    reason_code: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    session.add(
        IctSetupAuditEvent(
            setup_id=setup_id,
            event_type=event_type,
            reason_code=reason_code,
            payload=payload or {},
        )
    )


async def aggregate_opportunity_stats(session: AsyncSession) -> dict[str, Any]:
    runs = await session.execute(select(AiAnalysisRun))
    all_runs = list(runs.scalars().all())
    stats: dict[str, int] = {
        "ai_reports": len(all_runs),
        "decision_long": 0,
        "decision_short": 0,
        "wait_long_plans": 0,
        "wait_short_plans": 0,
        "true_no_trade": 0,
    }
    for r in all_runs:
        d = (r.decision or "").upper()
        final = (getattr(r, "final_ai_decision", None) or "").upper()
        mode = (getattr(r, "setup_mode", None) or "").lower()
        if final == "WAIT" or mode == "conditional":
            pot = ((r.structured or {}).get("potential_direction") or "").upper()
            if pot == "LONG":
                stats["wait_long_plans"] += 1
            elif pot == "SHORT":
                stats["wait_short_plans"] += 1
        elif d == "LONG":
            stats["decision_long"] += 1
        elif d == "SHORT":
            stats["decision_short"] += 1
        elif d == "NO_TRADE":
            stats["true_no_trade"] += 1

    audit_counts = await session.execute(
        select(IctSetupAuditEvent.event_type, func.count())
        .group_by(IctSetupAuditEvent.event_type)
    )
    for event_type, count in audit_counts.all():
        stats[f"audit_{event_type}"] = int(count)

    pos_counts = await session.execute(
        select(SimPosition.status, func.count()).group_by(SimPosition.status)
    )
    for status, count in pos_counts.all():
        stats[f"sim_position_{status}"] = int(count)

    stats["generated_at"] = datetime.now(timezone.utc).isoformat()
    return stats
