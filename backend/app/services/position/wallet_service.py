from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SimPosition, SimWalletLedger

LEDGER_DEPOSIT = "deposit"
LEDGER_WITHDRAW = "withdraw"
LEDGER_MARGIN_RESERVE = "margin_reserve"
LEDGER_MARGIN_RELEASE = "margin_release"
LEDGER_OPEN_FEE = "open_fee"
LEDGER_CLOSE_FEE = "close_fee"
LEDGER_FUNDING = "funding_fee"
LEDGER_REALIZED_PNL = "realized_pnl"
LEDGER_INITIAL = "initial_balance"


class WalletError(Exception):
    pass


async def _latest_balance(session: AsyncSession) -> float:
    row = await session.execute(select(func.max(SimWalletLedger.id)))
    max_id = row.scalar()
    if not max_id:
        return 0.0
    result = await session.execute(select(SimWalletLedger).where(SimWalletLedger.id == max_id))
    entry = result.scalar_one()
    return float(entry.balance_after)


async def append_ledger(
    session: AsyncSession,
    *,
    entry_type: str,
    amount_usdt: float,
    reference_type: str | None = None,
    reference_id: str | None = None,
    note: str | None = None,
    affects_balance: bool | None = None,
) -> SimWalletLedger:
    if affects_balance is None:
        affects_balance = entry_type not in (LEDGER_MARGIN_RESERVE, LEDGER_MARGIN_RELEASE)
    balance = await _latest_balance(session)
    delta = amount_usdt if affects_balance else 0.0
    new_balance = round(balance + delta, 8)
    if entry_type == LEDGER_WITHDRAW and new_balance < 0:
        raise WalletError("موجودی کافی برای برداشت نیست")
    row = SimWalletLedger(
        entry_type=entry_type,
        amount_usdt=round(amount_usdt, 8),
        balance_after=new_balance,
        reference_type=reference_type,
        reference_id=reference_id,
        note=note,
    )
    session.add(row)
    await session.flush()
    return row


async def margin_in_use(session: AsyncSession) -> float:
    open_statuses = ("pending_entry", "open")
    result = await session.execute(
        select(func.coalesce(func.sum(SimPosition.margin_usdt), 0.0)).where(
            SimPosition.status.in_(open_statuses)
        )
    )
    return float(result.scalar() or 0.0)


async def sum_fees(session: AsyncSession) -> dict[str, float]:
    open_f = await session.execute(
        select(func.coalesce(func.sum(SimWalletLedger.amount_usdt), 0.0)).where(
            SimWalletLedger.entry_type == LEDGER_OPEN_FEE
        )
    )
    close_f = await session.execute(
        select(func.coalesce(func.sum(SimWalletLedger.amount_usdt), 0.0)).where(
            SimWalletLedger.entry_type == LEDGER_CLOSE_FEE
        )
    )
    fund_f = await session.execute(
        select(func.coalesce(func.sum(SimWalletLedger.amount_usdt), 0.0)).where(
            SimWalletLedger.entry_type == LEDGER_FUNDING
        )
    )
    return {
        "opening_fees": abs(float(open_f.scalar() or 0.0)),
        "closing_fees": abs(float(close_f.scalar() or 0.0)),
        "funding_fees": abs(float(fund_f.scalar() or 0.0)),
    }


async def sum_realized_pnl(session: AsyncSession) -> float:
    result = await session.execute(
        select(func.coalesce(func.sum(SimWalletLedger.amount_usdt), 0.0)).where(
            SimWalletLedger.entry_type == LEDGER_REALIZED_PNL
        )
    )
    return float(result.scalar() or 0.0)


async def wallet_summary(session: AsyncSession, unrealized_pnl: float = 0.0) -> dict:
    balance = await _latest_balance(session)
    margin_used = await margin_in_use(session)
    available = balance - margin_used
    fees = await sum_fees(session)
    realized = await sum_realized_pnl(session)
    equity = balance + unrealized_pnl
    return {
        "wallet_balance": round(balance, 4),
        "available_balance": round(available, 4),
        "margin_in_use": round(margin_used, 4),
        "unrealized_pnl": round(unrealized_pnl, 4),
        "realized_pnl": round(realized, 4),
        "total_fees": round(fees["opening_fees"] + fees["closing_fees"] + fees["funding_fees"], 4),
        "fees_breakdown": fees,
        "account_equity": round(equity, 4),
    }


async def list_ledger(session: AsyncSession, limit: int = 200) -> list[SimWalletLedger]:
    result = await session.execute(
        select(SimWalletLedger).order_by(SimWalletLedger.id.desc()).limit(limit)
    )
    return list(result.scalars().all())
