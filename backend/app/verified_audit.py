"""Read-only historical diagnostic reconstruction, NOT a trading backtest.

python -m app.verified_audit --first-run 765 --last-run 776
Uses public native Bitunix candles and read-only SQLite. No API model or orders.
"""
import argparse
import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.config import get_effective_settings
from app.services.market_data import MarketDataService
from app.services.verified_trade_plan import build_candidates, valid_bars

STEPS = {'1m':60000,'5m':300000,'15m':900000,'1h':3600000,'4h':14400000}


def timestamp(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()*1000)


async def history(market, tf, first, end):
    """Bounded native pagination; never synthesize or fetch candles after end."""
    cursor = end//STEPS[tf]*STEPS[tf]
    bars = {}
    for _ in range(32):
        page = await market._fetch_klines_window('BTCUSDT',tf,limit=200,end_time_ms=cursor)
        if not page:
            raise ValueError('history_unavailable_'+tf)
        oldest = min(b.time_ms for b in page)
        if oldest >= cursor:
            raise ValueError('history_not_advancing_'+tf)
        for b in page:
            if first <= b.time_ms and b.time_ms+STEPS[tf] <= end:
                bars[b.time_ms] = b
        if oldest <= first:
            return sorted(bars.values(),key=lambda b:b.time_ms)
        cursor = oldest
        await asyncio.sleep(.15)
    raise ValueError('history_page_limit_'+tf)


def evaluate(series, costs, now, limits):
    windows = {tf:[b for b in series.get(tf,[]) if b.time_ms+step<=now][-limits[tf]:]
               for tf,step in STEPS.items()}
    quality = {tf:valid_bars(windows[tf],step,now,30 if tf in ('5m','15m','1h') else 20)
               for tf,step in STEPS.items()}
    if not all(quality.values()):
        return {'status':'data_blocked','quality':quality}
    # Historical live ticks are not archived. This proxy can affect late-stage gates.
    quote = windows['1m'][-1].close
    audit = {}
    plans,reasons = build_candidates(windows,costs,now,quote,audit=audit)
    return {'status':'reconstructed','quality':quality,'price_proxy':quote,
            'candidate_count':len(plans),'candidate_ids':[p['candidate_id'] for p in plans],
            'rejections':reasons,'candidate_audit':audit}


async def run(args):
    if args.first_run > args.last_run or args.last_run-args.first_run > 300:
        raise ValueError('Select at most 301 consecutive run IDs')
    with sqlite3.connect(Path(args.db).resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        rows = [dict(r) for r in db.execute('''SELECT id,created_at,completed_at,validation,structured
            FROM ai_analysis_runs WHERE id BETWEEN ? AND ? ORDER BY id''',
            (args.first_run,args.last_run))]
        cost = db.execute('''SELECT default_margin_usdt,default_leverage,maker_fee_rate,
            taker_fee_rate,slippage_bps,default_tp_behavior FROM sim_position_settings WHERE id=1''').fetchone()
        if not rows or cost is None:
            raise ValueError('Runs or paper cost settings missing')
        costs = SimpleNamespace(**dict(cost))
    # Prefer persisted diagnostic evidence when this version generated the run.
    results = []
    pending = []
    for row in rows:
        validation = json.loads(row['validation'] or '{}')
        structured = json.loads(row['structured'] or '{}')
        audit = validation.get('candidate_audit') or (structured.get('verified_plan_review') or {}).get('candidate_audit')
        if audit:
            results.append({'run_id':row['id'],'status':'recorded_audit','candidate_audit':audit})
        else:
            row['as_of_ms'] = timestamp(row['completed_at'] or row['created_at'])
            pending.append(row)
    if pending:
        settings = get_effective_settings()
        limits = {tf:getattr(settings,'ai_candle_limit_'+tf) for tf in STEPS}
        first = min(r['as_of_ms'] for r in pending)
        end = max(r['as_of_ms'] for r in pending)
        if end-first>86400000:
            raise ValueError('Historical reconstruction limited to one day per invocation')
        market = MarketDataService()
        series = {}
        for tf,step in STEPS.items():
            series[tf] = await history(market,tf,first//step*step-(limits[tf]+1)*step,end)
        for row in pending:
            result = evaluate(series,costs,row['as_of_ms'],limits)
            results.append({'run_id':row['id'],'as_of_ms':row['as_of_ms'],**result})
    return {'method':'diagnostic_reconstruction_not_performance',
            'limitations':['Historical reconstruction uses completion time, current configured candle limits and current Paper costs.',
                'Latest closed 1m price is a proxy for the unrecorded live tick. Historical exchange candles may be revised.',
                'Counts are branch events, not unique setups or hypothetical profitable trades. No AI approvals are replayed.'],
            'cost_assumptions':vars(costs),'runs':sorted(results,key=lambda r:r['run_id'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',default='/app/data/tradeonbu.db')
    parser.add_argument('--first-run',type=int,required=True)
    parser.add_argument('--last-run',type=int,required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args)),ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
