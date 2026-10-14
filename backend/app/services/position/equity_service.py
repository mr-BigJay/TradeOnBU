from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SimEquitySnapshot
from app.services.position import wallet_service as wallet
from app.services.position.mark_to_market import aggregate_open_mark_to_market
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.position.position_service import SimPositionService


async def record_equity_snapshot(
    session: AsyncSession, svc: "SimPositionService | None" = None
) -> SimEquitySnapshot:
    from app.services.position.position_service import SimPositionService

    svc = svc or SimPositionService()
    mtm = await aggregate_open_mark_to_market(session, svc)
    summary = await wallet.wallet_summary(
        session,
        unrealized_gross=mtm["unrealized_gross"],
        estimated_exit_fees=mtm["estimated_exit_fees"],
    )
    row = SimEquitySnapshot(
        equity_usdt=summary["account_equity"],
        wallet_balance=summary["wallet_balance"],
        margin_in_use=summary["margin_in_use"],
        unrealized_pnl=summary["unrealized_pnl"],
        recorded_at=datetime.now(timezone.utc),
    )
    session.add(row)
    await session.flush()
    return row
