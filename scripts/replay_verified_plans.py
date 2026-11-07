#!/usr/bin/env python3
"""Offline component replay. Never calls AI/exchange or touches the production DB.
Usage: PYTHONPATH=backend python scripts/replay_verified_plans.py replay.json.gz
Invalid source candles abort before any performance result is calculated.
"""
import argparse
import bisect
import gzip
import json
import math
from collections import Counter
from dataclasses import replace
from types import SimpleNamespace

from app.domain.candles import Candle
from app.domain.candle_quality import acceptable_prices, carried_open
from app.services.verified_trade_plan import build_candidates, STEP


def audit(rows):
    bad=[];gaps=[];carried=[];previous=None
    for i,row in enumerate(rows):
        try:
            t,o,h,l,c,*_=row
            bar=Candle(o,h,l,c,t)
            aligned=isinstance(t,int) and t%60000==0
            good=aligned and acceptable_prices(bar,previous,60000)
            if aligned and carried_open(bar,previous,60000):carried.append(i)
            if previous is not None and t-previous.time_ms!=60000:gaps.append(i)
            previous=bar
        except (TypeError,ValueError):
            good=False;previous=None;gaps.append(i)
        if not good:bad.append(i)
    return {'candles':len(rows),'invalid_ohlc_count':len(bad),'invalid_sample_indices':bad[:10],
            'carried_open_count':len(carried),'carried_open_sample_indices':carried[:10],
            'discontinuities':len(gaps),'valid':bool(rows) and not bad and not gaps}


def aggregate(rows,step):
    groups={}
    for t,o,h,l,c,*_ in rows:groups.setdefault(t//step*step,[]).append((t,o,h,l,c))
    out=[]
    for t,b in groups.items():
        if len(b)!=step//60000 or b[0][0]!=t or b[-1][0]!=t+step-60000:continue
        bar=Candle(b[0][1],max(x[2] for x in b),min(x[3] for x in b),b[-1][4],t)
        out.append(replace(bar,carried_open_verified=carried_open(bar,out[-1] if out else None,step)))
    return out


def replay(rows):
    quality=audit(rows)
    if not quality['valid']:
        return {'status':'blocked_untrusted_source','quality':quality,'performance':None}
    cost=SimpleNamespace(default_margin_usdt=50,default_leverage=3,maker_fee_rate=.0002,
        taker_fee_rate=.0005,slippage_bps=1,default_tp_behavior='market')
    series={tf:aggregate(rows,step) for tf,step in [('5m',STEP),('15m',3*STEP),('1h',12*STEP)]}
    times={tf:[b.time_ms for b in bars] for tf,bars in series.items()}
    seen=set();pending=None;outcomes=[];rejects=Counter();entry_ms=None
    minute_history=[]
    for t,o,h,l,c,*_ in rows:
        bar=Candle(o,h,l,c,t)
        minute_history.append(replace(bar,carried_open_verified=carried_open(bar,minute_history[-1] if minute_history else None,60000)))
        now=t+60000
        if pending:
            sign=1 if pending['direction']=='LONG' else -1
            sl=l<=pending['stop'] if sign==1 else h>=pending['stop']
            tp=h>=pending['target'] if sign==1 else l<=pending['target']
            hit=l<=pending['entry']<=h
            result=None
            if entry_ms is None:
                if t>=pending['expires_ms']:result='expired'
                elif hit:
                    entry_ms=t
                    if sl or tp:result='ambiguous'
                elif sl or tp:result='invalid_before_entry'
            elif sl or tp:
                result='ambiguous' if sl and tp else 'sl' if sl else 'tp'
            if result:
                outcomes.append({'candidate_id':pending['candidate_id'],'registered_ms':pending['registered_ms'],
                    'entry_ms':entry_ms,'end_ms':now,'outcome':result})
                pending=None;entry_ms=None
        # Scan 60s after each closed five-minute boundary, just like scheduler.
        if pending or now%STEP!=60000:continue
        cutoff=now//STEP*STEP
        windows={}
        for tf,step in [('5m',STEP),('15m',3*STEP),('1h',12*STEP)]:
            end=bisect.bisect_right(times[tf],now-step)
            windows[tf]=series[tf][max(0,end-120):end]
        windows['1m']=minute_history[-120:]
        plans,reasons=build_candidates(windows,cost,now,c)
        rejects.update(reasons)
        for p in plans:
            if p['opportunity_id'] in seen:continue
            seen.add(p['opportunity_id']);pending={**p,'registered_ms':now};break
    if pending:outcomes.append({'candidate_id':pending['candidate_id'],'entry_ms':entry_ms,'outcome':'censored'})
    return {'status':'component_replay_only','quality':quality,
        'engine_version':'verified_reversal_v1','evaluates_multi_setup_analyst':False,
        'method':'Candle engine only; API approvals NOT simulated; one position at a time; no portfolio return claim.',
        'cost_assumptions':vars(cost),'outcomes':outcomes,'counts':dict(Counter(x['outcome'] for x in outcomes)),
        'rejections':dict(rejects),'performance':None}


def main():
    p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('--output');args=p.parse_args()
    opener=gzip.open if args.source.endswith('.gz') else open
    with opener(args.source,'rt') as f:data=json.load(f)
    result=replay(data['candles_1m'])
    raw=json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:
        with open(args.output,'w') as f:f.write(raw+'\n')
    print(raw)
    return 2 if result['status']=='blocked_untrusted_source' else 0

if __name__=='__main__':raise SystemExit(main())
