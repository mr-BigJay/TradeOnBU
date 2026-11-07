"""Observed closed-trade metrics; never substitute old-engine replay for AI results."""
from collections import defaultdict


def summarize(trades):
    groups=defaultdict(list)
    for trade in trades:
        extra=trade.extra or {}
        contract=extra.get('verified_trade_plan') or {}
        execution=(extra.get('execution_plan') or {}).get('version','legacy_execution')
        key=(contract.get('version','legacy_or_manual'),execution,extra.get('strategy_id','unknown'))
        groups[key].append(trade)
    results=[]
    for (version,execution,strategy),rows in sorted(groups.items()):
        valid=[r for r in rows if r.close_reason!='ambiguous_intrabar' and (r.extra or {}).get('outcome_valid') is not False]
        valid.sort(key=lambda r:(r.closed_at.isoformat() if r.closed_at else '',r.public_id))
        pnls=[float(r.net_pnl_usdt or 0) for r in valid]
        profits=sum(max(p,0) for p in pnls);losses=-sum(min(p,0) for p in pnls)
        cumulative=peak=drawdown=0.0
        for p in pnls:
            cumulative+=p;peak=max(peak,cumulative);drawdown=max(drawdown,peak-cumulative)
        results.append(dict(version=version,execution_version=execution,strategy_id=strategy,closed_trades=len(rows),
            valid_trades=len(valid),excluded_trades=len(rows)-len(valid),
            wins=sum(p>0 for p in pnls),losses=sum(p<0 for p in pnls),
            win_rate_pct=100*sum(p>0 for p in pnls)/len(pnls) if pnls else None,
            net_pnl_usdt=round(cumulative,4),
            net_profit_factor=profits/losses if losses>0 else None,
            expectancy_usdt=cumulative/len(pnls) if pnls else None,
            realized_drawdown_usdt=round(drawdown,4),
            drawdown_basis='closed trade net PnL; excludes unrealized equity and transfers'))
    return results
