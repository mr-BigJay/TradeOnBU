from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.tehran_time import format_tehran, parse_tehran_date, report_period_bounds_utc
from app.models import SimEquitySnapshot, SimPosition
from app.services.position import wallet_service as wallet


CLOSED_STATUSES = ("closed", "tp_hit", "sl_hit", "liquidated")


def _reportable_trades(trades: list[SimPosition]) -> list[SimPosition]:
    """Exclude ambiguous / invalid outcomes from performance statistics."""
    out = []
    for t in trades:
        if (t.close_reason or "") == "ambiguous_intrabar":
            continue
        extra = t.extra or {}
        if extra.get("outcome_valid") is False:
            continue
        out.append(t)
    return out


async def build_report(
    session: AsyncSession,
    *,
    period: str = "all",
    start: datetime | None = None,
    end: datetime | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    if period == "custom":
        if start_date and end_date:
            range_start, range_end = report_period_bounds_utc(
                period,
                custom_start=parse_tehran_date(start_date),
                custom_end=parse_tehran_date(end_date),
            )
        elif start and end:
            range_start, range_end = start, end
        else:
            range_start, range_end = None, None
    else:
        range_start, range_end = report_period_bounds_utc(period)

    q = select(SimPosition).where(SimPosition.status.in_(CLOSED_STATUSES))
    if range_start:
        q = q.where(SimPosition.closed_at >= range_start)
    if range_end:
        q = q.where(SimPosition.closed_at <= range_end)
    result = await session.execute(q.order_by(SimPosition.closed_at.desc()))
    trades = list(result.scalars().all())
    stats_trades = _reportable_trades(trades)

    wins = [t for t in stats_trades if (t.net_pnl_usdt or 0) > 0]
    losses = [t for t in stats_trades if (t.net_pnl_usdt or 0) < 0]

    gross_profit = sum(t.gross_pnl_usdt or 0 for t in stats_trades if (t.gross_pnl_usdt or 0) > 0)
    gross_loss = sum(abs(t.gross_pnl_usdt or 0) for t in stats_trades if (t.gross_pnl_usdt or 0) < 0)
    net = sum(t.net_pnl_usdt or 0 for t in stats_trades)
    fees = await wallet.sum_fees(session)

    rr_vals = []
    for t in stats_trades:
        entry = t.fill_price or t.entry_price
        if entry and t.stop_loss and t.take_profit:
            risk = abs(entry - t.stop_loss)
            reward = abs(t.take_profit - entry)
            if risk > 0:
                rr_vals.append(reward / risk)
    avg_rr = sum(rr_vals) / len(rr_vals) if rr_vals else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0)

    equity_q = select(SimEquitySnapshot).order_by(SimEquitySnapshot.recorded_at.asc())
    if range_start:
        equity_q = equity_q.where(SimEquitySnapshot.recorded_at >= range_start)
    if range_end:
        equity_q = equity_q.where(SimEquitySnapshot.recorded_at <= range_end)
    equity_rows = await session.execute(equity_q)
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
                "source_label": (t.extra or {}).get("source_label"),
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
        "total_trades": len(stats_trades),
        "total_closes_including_invalid": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": round(len(wins) / len(stats_trades) * 100, 2) if stats_trades else 0.0,
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
