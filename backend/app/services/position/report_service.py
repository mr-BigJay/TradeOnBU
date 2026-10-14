from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.tehran_time import format_tehran
from app.models import SimEquitySnapshot, SimPosition
from app.services.position import wallet_service as wallet


CLOSED_STATUSES = ("closed", "tp_hit", "sl_hit", "liquidated", "cancelled")


def _parse_range(
    period: str,
    start: datetime | None,
    end: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    now = datetime.now(timezone.utc)
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, now
    if period == "7d":
        from datetime import timedelta

        return now - timedelta(days=7), now
    if period == "30d":
        from datetime import timedelta

        return now - timedelta(days=30), now
    if period == "custom":
        return start, end
    return None, None


async def build_report(
    session: AsyncSession,
    *,
    period: str = "all",
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict:
    range_start, range_end = _parse_range(period, start, end)
    q = select(SimPosition).where(SimPosition.status.in_(("closed", "tp_hit", "sl_hit", "liquidated")))
    if range_start:
        q = q.where(SimPosition.closed_at >= range_start)
    if range_end:
        q = q.where(SimPosition.closed_at <= range_end)
    result = await session.execute(q.order_by(SimPosition.closed_at.desc()))
    trades = list(result.scalars().all())

    wins = [t for t in trades if (t.net_pnl_usdt or 0) > 0]
    losses = [t for t in trades if (t.net_pnl_usdt or 0) < 0]
    gross_profit = sum(t.gross_pnl_usdt or 0 for t in wins)
    gross_loss = sum(abs(t.gross_pnl_usdt or 0) for t in losses)
    net = sum(t.net_pnl_usdt or 0 for t in trades)
    fees = await wallet.sum_fees(session)

    rr_vals = []
    for t in trades:
        entry = t.fill_price or t.entry_price
        if entry and t.stop_loss and t.take_profit:
            risk = abs(entry - t.stop_loss)
            reward = abs(t.take_profit - entry)
            if risk > 0:
                rr_vals.append(reward / risk)
    avg_rr = sum(rr_vals) / len(rr_vals) if rr_vals else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0)

    equity_rows = await session.execute(select(SimEquitySnapshot).order_by(SimEquitySnapshot.recorded_at.asc()))
    equity_history = [
        {
            "equity_usdt": r.equity_usdt,
            "recorded_at_tehran": format_tehran(r.recorded_at),
            "recorded_at_utc": r.recorded_at.isoformat(),
        }
        for r in equity_rows.scalars().all()
    ]
    max_dd = 0.0
    peak = 0.0
    for point in equity_history:
        v = point["equity_usdt"]
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak * 100)

    history = []
    for t in trades:
        history.append(
            {
                "symbol": t.symbol,
                "direction": t.direction,
                "margin_usdt": t.margin_usdt,
                "leverage": t.leverage,
                "entry_price": t.fill_price or t.entry_price,
                "exit_price": t.exit_price,
                "stop_loss": t.stop_loss,
                "take_profit": t.take_profit,
                "opened_at_tehran": format_tehran(t.opened_at),
                "closed_at_tehran": format_tehran(t.closed_at),
                "gross_pnl_usdt": t.gross_pnl_usdt,
                "fees_usdt": (t.entry_fee_usdt or 0) + (t.exit_fee_usdt or 0) + (t.funding_fee_usdt or 0),
                "net_pnl_usdt": t.net_pnl_usdt,
                "close_reason": t.close_reason,
                "ai_setup_id": t.ai_setup_id,
                "public_id": t.public_id,
            }
        )

    summary = await wallet.wallet_summary(session)
    return {
        "period": period,
        "total_trades": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "gross_profits": round(gross_profit, 4),
        "gross_losses": round(gross_loss, 4),
        "fees": fees,
        "net_profit_loss": round(net, 4),
        "average_rr": round(avg_rr, 4),
        "profit_factor": round(profit_factor, 4),
        "max_drawdown_pct": round(max_dd, 4),
        "realized_pnl": summary["realized_pnl"],
        "unrealized_pnl": summary["unrealized_pnl"],
        "account_equity": summary["account_equity"],
        "equity_history": equity_history,
        "history": history,
    }


def history_to_csv(rows: list[dict]) -> str:
    headers = [
        "symbol",
        "direction",
        "margin",
        "leverage",
        "entry",
        "exit",
        "sl",
        "tp",
        "opened_tehran",
        "closed_tehran",
        "gross_pnl",
        "fees",
        "net_pnl",
        "close_reason",
        "ai_setup_id",
    ]
    lines = [",".join(headers)]
    for r in rows:
        lines.append(
            ",".join(
                str(r.get(k.replace("_tehran", "_at_tehran") if k == "opened_tehran" else k, ""))
                for k in [
                    "symbol",
                    "direction",
                    "margin_usdt",
                    "leverage",
                    "entry_price",
                    "exit_price",
                    "stop_loss",
                    "take_profit",
                    "opened_at_tehran",
                    "closed_at_tehran",
                    "gross_pnl_usdt",
                    "fees_usdt",
                    "net_pnl_usdt",
                    "close_reason",
                    "ai_setup_id",
                ]
            )
        )
    return "\n".join(lines)
