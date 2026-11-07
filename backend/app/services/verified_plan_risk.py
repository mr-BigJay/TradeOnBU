"""Paper-only risk budget for verified plans; shared account exposure is counted."""
from datetime import timezone
from sqlalchemy import select
from app.models import SimPosition
from app.services.position.wallet_service import wallet_summary


def risk_margin(*, equity, available, entry, stop, direction, maker, taker, slippage_bps, leverage, cap):
    """0.25% equity max loss, including entry fee and adverse stop execution."""
    import math
    vals=(equity,available,entry,stop,maker,taker,slippage_bps,leverage,cap)
    if not all(math.isfinite(float(x)) for x in vals) or min(equity,available,entry,stop,leverage,cap)<=0 or min(maker,taker,slippage_bps)<0:
        return 0.0
    sign=1 if direction=='LONG' else -1
    exit_price=stop*(1-sign*slippage_bps/10000)
    loss_per_unit=sign*(entry-exit_price)+entry*maker+exit_price*taker
    if loss_per_unit<=0:
        return 0.0
    margin=min(equity*.0025/loss_per_unit*entry/leverage, available*.95, cap)
    return math.floor(margin*10000)/10000


async def budget(session, settings, setup, now, *, execution_stop=None):
    positions=(await session.execute(select(SimPosition).where(SimPosition.status.in_(('open','pending_entry'))))).scalars().all()
    if positions:
        return None,'verified_one_position_limit'
    summary=await wallet_summary(session)
    equity=float(summary['account_equity']);available=float(summary['available_balance'])
    start=now.astimezone(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0)
    closed=(await session.execute(select(SimPosition).where(SimPosition.closed_at>=start,SimPosition.status!='cancelled'))).scalars().all()
    losses=sum(min(0,float(p.net_pnl_usdt or 0)) for p in closed)
    start_equity=equity-sum(float(p.net_pnl_usdt or 0) for p in closed)
    if start_equity<=0 or -losses>=start_equity*.01:
        return None,'verified_daily_loss_limit'
    leverage=min(3,int(settings.default_leverage))
    margin=risk_margin(equity=equity,available=available,entry=setup.entry_price,stop=execution_stop if execution_stop is not None else setup.stop_loss,
        direction=setup.direction,maker=settings.maker_fee_rate,taker=settings.taker_fee_rate,
        slippage_bps=settings.slippage_bps,leverage=leverage,
        cap=min(float(settings.default_margin_usdt),float(settings.max_margin_per_position)))
    if margin<=0:
        return None,'verified_no_risk_budget'
    return {'margin':margin,'leverage':leverage,'risk_fraction':.0025,'equity':equity},None
