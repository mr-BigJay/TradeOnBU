from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SimEquitySnapshot
from app.services.position import wallet_service as wallet


async def record_equity_snapshot(session: AsyncSession, *, unrealized_pnl: float = 0.0) -> SimEquitySnapshot:
    summary = await wallet.wallet_summary(session, unrealized_pnl=unrealized_pnl)
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
