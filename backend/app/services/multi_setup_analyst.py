"""AI discovers conditional scenarios; server binds prices to closed-candle evidence.
No model-supplied booleans constitute entry confirmation. Paper only.
"""
import hashlib
import json
import os
from datetime import timedelta
from sqlalchemy import select
from app.services.poi_context import market_zones
from app.services.verified_trade_plan import atr, trend, valid_bars, flat, _tick
from app.services.ict_execution_plan import target_at_net_rr

VERSION = 'multi_setup_analyst_v1'
STEPS = {'1m':60000,'5m':300000,'15m':900000,'1h':3600000,'4h':14400000}
PROFILES = {'ict_reversal':'sweep_mss_poi', 'trend_continuation':'trend_pullback_resume', 'breakout_retest':'level_breakout_retest'}
TRIGGERS = {
 'ict_reversal':'Sweep و سپس MSS روی کندل بستهٔ 5m؛ سفارش محدود برای بازگشت بعدی به ناحیه.',
 'trend_continuation':'اصلاح به ناحیه و سپس بسته‌شدن کندل 5m در جهت روند، فراتر از سقف/کف کندل قبل؛ سفارش محدود برای بازگشت بعدی.',
 'breakout_retest':'عبور قیمت بسته‌شدن 5m از سطح، سپس بازآزمایی و بسته‌شدن در سمت شکست؛ سفارش محدود برای بازگشت بعدی.',
}
INSTRUCTIONS = '''You are a multi-scenario BTCUSDT analyst, not a reviewer of preselected reversal candidates.
Compare trend continuation, reversal, and breakout/retest using 4H/1H context and 15m/5m execution.
Use the provided closed candles, evidence_catalog, and freshness-filtered market_intelligence.
Return ONLY JSON with market_summary_fa, comparison_fa, and plans (0 to 3 objects, ranked best first).
Each plan: strategy_id (ict_reversal|trend_continuation|breakout_retest), direction (LONG|SHORT),
zone_id, invalidation_id, target_id (exact supplied catalog IDs), reason_fa.
At most one plan per strategy. Only enabled strategies. Empty plans is a valid outcome.
Select meaningful candle-backed areas; NEVER invent price levels, IDs, confidence probabilities or confirmations.
Continuation requires aligned 1H context without opposing 4H; breakout uses a resistance for LONG or support for SHORT.
Explain why each selected scenario is preferable and why alternatives were not selected.
Server derives midpoint limit entry, ATR-buffered structural stop and cost-adjusted target before the nearest obstacle.
Server monitors these EXACT conditions after registration: reversal=sweep then MSS; continuation=zone pullback
then a directional close beyond prior 5m high/low; breakout=5m close crossing the level then a retest holding it.
All require a later limit fill. Do not describe additional unimplemented conditions as executable requirements.
A proposal is NOT an entry signal and none of these events are assumed completed. No market-on-touch orders.
Respect source/timeframe/venue caveats; missing optional evidence is not a directional signal. No return promises.
'''

def enabled():
    return os.getenv('MULTI_SETUP_ANALYST_ENABLED','0') == '1'


def catalog(series, now_ms):
    for tf,step in STEPS.items():
        if not valid_bars(series.get(tf,[]),step,now_ms,30):
            raise ValueError('invalid_or_stale_'+tf)
    zones=market_zones({k:series[k] for k in ('5m','15m','1h','4h')})
    items=[]
    def add(kind,tf,t,lo,hi,direction=None,price=None):
        item=dict(kind=kind,timeframe=tf,time_ms=t,low=lo,high=hi,price=price if price is not None else (lo+hi)/2,direction=direction)
        item['id']=hashlib.sha256(json.dumps(item,sort_keys=True).encode()).hexdigest()[:20]
        items.append(item)
    for x in zones['support_resistance']:
        width=max(.15*atr(series[x['timeframe']]),.1)
        add(x['kind'],x['timeframe'],x['time_ms'],x['price']-width,x['price']+width,price=x['price'])
    for x in zones['order_blocks']:
        if x['status']!='invalidated':
            add('OB',x['timeframe'],x['origin_ms'],x['low'],x['high'],x['direction'])
    for tf in ('5m','15m'):
        bars=series[tf]
        gaps=[]
        for i in range(2,len(bars)):
            a,b=bars[i-2],bars[i]
            for direction,lo,hi in (('LONG',a.high,b.low),('SHORT',b.high,a.low)):
                if lo<hi and not any((x.close<lo if direction=='LONG' else x.close>hi) for x in bars[i+1:]):
                    gaps.append((direction,lo,hi,b.time_ms))
        for direction,lo,hi,t in gaps[-8:]:add('FVG',tf,t,lo,hi,direction)
    return items


async def review_allowed(session, now):
    """One paid attempt per UTC four-hour slot, including failures/manual clicks."""
    from app.models import AiAnalysisRun
    slot=int(now.timestamp()*1000)//14400000*14400000
    rows=(await session.execute(select(AiAnalysisRun).where(AiAnalysisRun.created_at>=now-timedelta(days=1)))).scalars().all()
    if any(((r.structured or {}).get('analyst_review') or {}).get('slot_ms')==slot for r in rows):
        return False,slot
    return True,slot


def compile_plan(proposal, evidence, series, costs, now_ms, observed_ms):
    if not isinstance(proposal,dict):raise ValueError('invalid_proposal')
    sid=proposal.get('strategy_id');direction=proposal.get('direction')
    if not isinstance(sid,str) or sid not in PROFILES or direction not in ('LONG','SHORT'):raise ValueError('invalid_strategy_direction')
    by_id={x['id']:x for x in evidence}
    try:z,inv,target=(by_id[proposal[k]] for k in ('zone_id','invalidation_id','target_id'))
    except (KeyError,TypeError):raise ValueError('unknown_evidence_id')
    sign=1 if direction=='LONG' else -1
    regime=trend(series['1h']);higher=trend(series['4h'])
    aligned='bullish' if sign==1 else 'bearish';opposed='bearish' if sign==1 else 'bullish'
    if sid=='trend_continuation' and (regime!=aligned or higher==opposed):raise ValueError('continuation_trend_conflict')
    if sid=='breakout_retest' and z['kind']!=('resistance' if sign==1 else 'support'):raise ValueError('breakout_requires_level')
    if z.get('direction') and z['direction']!=direction:raise ValueError('zone_direction_conflict')
    entry=round((z['low']+z['high'])/2,1)
    invalidation=inv['low'] if sign==1 else inv['high']
    if sign*((z['low'] if sign==1 else z['high'])-invalidation)<=0:raise ValueError('invalidation_inside_zone')
    if sign*(series['1m'][-1].close-invalidation)<=0:raise ValueError('already_invalidated')
    if target['kind']!=('resistance' if sign==1 else 'support'):raise ValueError('target_requires_structural_level')
    vol=atr(series['5m'])
    stop=_tick(invalidation-sign*max(.25*vol,.1),sign)
    if sign*(entry-stop)<vol:stop=_tick(entry-sign*vol,sign)
    if not 0<sign*(entry-stop)<=3*atr(series['15m']):raise ValueError('stop_too_wide')
    obstacle=target['price']-sign*.1*vol
    if sign*(obstacle-entry)<=0:raise ValueError('target_wrong_side')
    obstacles=[x['price']-sign*.1*vol for x in evidence if x['timeframe'] in ('15m','1h','4h')
               and x['kind']==('resistance' if sign==1 else 'support') and sign*(x['price']-entry)>.1*vol]
    if obstacles:obstacle=min([obstacle,*obstacles]) if sign==1 else max([obstacle,*obstacles])
    tp=target_at_net_rr(costs,direction,entry,stop,obstacle)
    if tp is None:raise ValueError('insufficient_net_reward_before_obstacle')
    if sign*(series['1m'][-1].close-tp)>=0:raise ValueError('target_already_reached')
    expiry=observed_ms+14400000
    if now_ms>=expiry:raise ValueError('proposal_expired')
    reason=str(proposal.get('reason_fa') or '')[:1600]
    if not reason.strip():raise ValueError('missing_reason')
    c=dict(version=VERSION,strategy_id=sid,confirmation_type=PROFILES[sid],direction=direction,
           entry=entry,zone=[z['low'],z['high']],stop=stop,target=tp,invalidation=invalidation,
           formed_ms=observed_ms,expires_ms=expiry,breakout_level=z['price'] if sid=='breakout_retest' else None,
           evidence={'zone':z,'invalidation':inv,'target':target,'atr14':vol,'obstacle':obstacle,
                     'hourly_regime':regime,'four_hour_regime':higher,'confirmed':False})
    c['opportunity_id']=f"{VERSION}:{sid}:{direction}:{z['id']}:{observed_ms//14400000}"
    c['candidate_id']=hashlib.sha256(json.dumps(c,sort_keys=True).encode()).hexdigest()[:24]
    c.update(as_of_ms=observed_ms,approved_at_ms=now_ms)
    return dict(strategy_id=sid,decision='NO_TRADE',final_ai_decision='WAIT',setup_mode='conditional',
        potential_direction=direction,entry_price=entry,entry_zone_low=z['low'],entry_zone_high=z['high'],
        conditional_entry_zone_low=z['low'],conditional_entry_zone_high=z['high'],stop_loss=stop,take_profit=tp,
        structural_invalidation_level=invalidation,confirmation_type=PROFILES[sid],confirmation_timeframe='5m',
        confirmation_required=TRIGGERS[sid],conditional_invalidation='بسته‌شدن 5m آن‌سوی سطح ابطال یا پایان اعتبار چهار‌ساعته.',
        breakout_level=c['breakout_level'],trade_reason=reason,market_regime=regime,market_regime_confidence='low',
        market_context_caution_ack=True,trade_against_dominant_trend=regime==opposed,
        setup_timeframe='intraday_5m_15m',liquidity_event_clear=False,structural_confirmation_clear=False,
        logical_invalidation_clear=True,poi_reaction_clear=False,
        proposed_setup={'new_setup':True,'thesis_fa':reason,'expires_hours':(expiry-now_ms)/3600000},
        verified_trade_plan=c,strategy_candidates={})


def compile_response(raw,evidence,series,costs,now_ms,observed_ms,strategies):
    if not isinstance(raw,dict) or not isinstance(raw.get('plans'),list) or len(raw['plans'])>3:
        raise ValueError('invalid_analyst_response')
    accepted={};rejections=[];seen=set()
    for p in raw['plans']:
        try:
            sid=p.get('strategy_id') if isinstance(p,dict) else None
            if not isinstance(sid,str) or sid not in PROFILES:raise ValueError('invalid_strategy_direction')
            if sid in seen:raise ValueError('duplicate_strategy')
            if not strategies.get(sid):raise ValueError('strategy_disabled')
            seen.add(sid)
            plan=compile_plan(p,evidence,series,costs,now_ms,observed_ms)
            accepted[sid]=plan
        except ValueError as exc:rejections.append(str(exc))
    result=next(iter(accepted.values())) if accepted else flat('پیشنهاد قابل اجرای معتبر وجود ندارد.')
    result['strategy_candidates']={sid:p for sid,p in accepted.items() if p is not result}
    result['analyst_summary']={'market_summary_fa':str(raw.get('market_summary_fa') or '')[:3000],
        'comparison_fa':str(raw.get('comparison_fa') or '')[:3000], 'proposed':len(raw['plans']),
        'accepted':len(accepted),'rejections':rejections}
    return result
