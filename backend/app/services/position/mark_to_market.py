from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from app.services.position.position_service import SimPositionService


async def aggregate_open_mark_to_market(
    session: AsyncSession, svc: "SimPositionService | None" = None
) -> dict[str, float]:
    from app.services.position.position_service import SimPositionService
    """
    Open-position mark-to-market for equity.

    Entry fees are already deducted from wallet_balance; unrealized uses gross price
    PnL minus estimated exit fees only (never entry fee again).
    """
    svc = svc or SimPositionService()
    gross = 0.0
    exit_est = 0.0
    for pos in await svc.list_active(session):
        if pos.status != "open":
            continue
        data = await svc.enrich_open(session, pos)
        metrics = data.get("metrics") or {}
        gross += float(metrics.get("gross_unrealized_pnl") or 0)
        exit_est += float(metrics.get("estimated_exit_fee_usdt") or 0)
    net = gross - exit_est
    return {
        "unrealized_gross": round(gross, 6),
        "estimated_exit_fees": round(exit_est, 6),
        "unrealized_net": round(net, 6),
    }
