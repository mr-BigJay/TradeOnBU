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
from app.services.analyst_regime import executable_regime
from app.services.analyst_outlook import compile_forecasts

VERSION = 'multi_setup_analyst_v1'
PROMPT_VERSION = 4
STEPS = {'1m':60000,'5m':300000,'15m':900000,'1h':3600000,'4h':14400000}
PROFILES = {'ict_reversal':'sweep_mss_poi', 'trend_continuation':'trend_pullback_resume', 'breakout_retest':'level_breakout_retest'}
TRIGGERS = {
 'ict_reversal':'Sweep و سپس MSS روی کندل بستهٔ 5m؛ سفارش محدود برای بازگشت بعدی به ناحیه.',
 'trend_continuation':'اصلاح به ناحیه و سپس بسته‌شدن کندل 5m در جهت روند، فراتر از سقف/کف کندل قبل؛ سفارش محدود برای بازگشت بعدی.',
 'breakout_retest':'عبور قیمت بسته‌شدن 5m از سطح، سپس بازآزمایی و بسته‌شدن در سمت شکست؛ سفارش محدود برای بازگشت بعدی.',
}
INSTRUCTIONS = '''You are a multi-scenario BTCUSDT analyst, not a reviewer of preselected reversal candidates.
Compare trend continuation, reversal, and breakout/retest using 4H/1H context and 15m/5m execution.
Use the provided closed candles, evidence_catalog, strategy_features, execution_regime and freshness-filtered market_intelligence.
The market.timeframes[*].candles rows use the explicit candles_columns order [time_ms,open,high,low,close,volume], with UTC epoch milliseconds. All 5m/15m/1h/4h bars are present; 1m includes the latest 45 closed minutes for sequencing only. time_iso is derivable from time_ms.
EMA9/20, RSI14, ATR, ADX and relative volume are descriptive context for comparing scenarios,
NEVER mandatory entry thresholds. RSI extremes or EMA crosses alone are not permission to trade.
Return ONLY JSON with market_summary_fa, comparison_fa, forecast_scenarios, and plans.
The four-hour outlook must explain the likely paths *conditionally*, NOT claim guaranteed price movements:
support rebound on a fresh/untested zone, resistance rejection, tested support breakdown with retest,
resistance breakout with retest, or pullback continuation, wherever actual candle evidence supports them.
Compare what happens IF price reaches a zone, IF it holds, and IF it breaks; include 1h/4h structural
trend, EMA9/20 direction, RSI14, ADX14, ATR14 and volume when available and relevant.
A previously tested area is NOT necessarily going to break: describe test count, displacement,
rejection strength and volume as context, not a quantified probability or a signal by themselves.
forecast_scenarios is 0 to 4 ranked watch-only objects, preferably 2+ distinct paths when supported.
Each: scenario_type (support_bounce|resistance_rejection|support_break|resistance_break|trend_pullback),
direction (LONG|SHORT), level_id (exact evidence_catalog ID), target_id (exact evidence_catalog ID or null),
reason_fa (why this area is significant, considering touch_count, status, HTF/LTF and relevant indicators).
Do not invent levels. The server resolves every forecast price, state, condition and invalidation from evidence.
All forecast_scenarios are a *watchlist*, NOT executable positions. Provide forward-looking levels even
when no executable plan passes the server rules. Never promise that an untouched area must reverse.
plans is 0 to 3 *independently executable conditional proposals* ranked best first.
Each plan: strategy_id (ict_reversal|trend_continuation|breakout_retest), direction (LONG|SHORT),
zone_id, invalidation_id, target_id (exact supplied catalog IDs), reason_fa.
At most one plan per strategy. Only enabled strategies. Empty plans is a valid outcome.
Select meaningful candle-backed areas; NEVER invent price levels, IDs, confidence probabilities or confirmations.
For continuation, use ONLY execution_regime.timeframes.1h.trend and .4h.trend as the executable trend: bullish/bearish are defined by two confirmed swing highs AND lows, NOT EMA slope or narrative bias. LONG requires 1h bullish and 4h not bearish; SHORT requires 1h bearish and 4h not bullish. If EMA or your visual impression disagrees, explicitly explain the discrepancy and do not propose a continuation that fails the swing rule. Breakout uses resistance for LONG or support for SHORT.
Explain why each selected scenario is preferable and why alternatives were not selected.
Server derives midpoint limit entry, ATR-buffered structural stop and cost-adjusted target before the nearest obstacle.
Server verifies these EXACT conditions from the most recent 15 minutes of closed evidence or forward after registration: reversal=sweep then MSS; continuation=zone pullback
then a directional close beyond prior 5m high/low; breakout=5m close crossing the level then a retest holding it.
For reversal, sweep the selected invalidation area's reference price and reclaim it;
MSS must close through the latest confirmed opposite 5m pivot, fixed when the plan is compiled.
All require a later limit fill. Do not describe additional unimplemented conditions as executable requirements.
A proposal is NOT an entry signal; only server-verified candles can mark events completed. No market-on-touch orders.
Respect source/timeframe/venue caveats; missing optional evidence is not a directional signal. No return promises.
LANGUAGE CONTRACT: Every field ending in _fa MUST be fluent Persian (Farsi), written in Persian script. Never use Armenian, Cyrillic, or another language. Before returning JSON, verify market_summary_fa, comparison_fa and every reason_fa are Persian. Keep technical abbreviations such as EMA, RSI, ADX, ATR, RVOL, MSS, BOS, FVG and OB in Latin script when useful.
'''

def enabled():
    return os.getenv('MULTI_SETUP_ANALYST_ENABLED','0') == '1'


def catalog(series, now_ms):
    for tf,step in STEPS.items():
        if not valid_bars(series.get(tf,[]),step,now_ms,30):
            raise ValueError('invalid_or_stale_'+tf)
    zones=market_zones({k:series[k] for k in ('5m','15m','1h','4h')})
    items=[]
    def add(kind,tf,t,lo,hi,direction=None,price=None,metadata=None):
        item=dict(kind=kind,timeframe=tf,time_ms=t,low=lo,high=hi,price=price if price is not None else (lo+hi)/2,direction=direction)
        item['id']=hashlib.sha256(json.dumps(item,sort_keys=True).encode()).hexdigest()[:20]
        item.update(metadata or {})
        items.append(item)
    for x in zones['support_resistance']:
        tf=x['timeframe']; width=max(.15*atr(series[tf]),.1)
        lo,hi=x['price']-width,x['price']+width
        # Only AFTER both confirmation candles have closed; touching the
        # originating swing must not count as a retest of its own level.
        confirmed_ms=x['time_ms']+3*STEPS[tf]
        later=[b for b in series[tf] if b.time_ms>=confirmed_ms]
        touches=[];in_touch=False
        for bar in later:
            touching=bar.low<=hi and bar.high>=lo
            if touching and not in_touch:
                touches.append(bar.time_ms)
            in_touch=touching
        add(x['kind'],tf,x['time_ms'],lo,hi,price=x['price'],metadata={
            'status':'retested' if touches else 'fresh',
            'touch_count':len(touches),'last_touch_ms':touches[-1] if touches else None,
            'confirmed_ms':confirmed_ms,'age_ms':now_ms-confirmed_ms,
        })
    for x in zones['order_blocks']:
        if x['status']!='invalidated':
            later=[b for b in series[x['timeframe']] if b.time_ms>x['confirmed_ms']]
            add('OB',x['timeframe'],x['origin_ms'],x['low'],x['high'],x['direction'],metadata={
                'status':x['status'],'confirmed_ms':x['confirmed_ms']+STEPS[x['timeframe']],
                'age_ms':now_ms-x['confirmed_ms']-STEPS[x['timeframe']],
                'touch_count':sum(b.low<=x['high'] and b.high>=x['low'] for b in later)})
    for tf in ('5m','15m'):
        bars=series[tf]
        gaps=[]
        for i in range(2,len(bars)):
            a,b=bars[i-2],bars[i]
            for direction,lo,hi in (('LONG',a.high,b.low),('SHORT',b.high,a.low)):
                if lo<hi and not any((x.close<lo if direction=='LONG' else x.close>hi) for x in bars[i+1:]):
                    later=bars[i+1:]
                    touches=sum(x.low<=hi and x.high>=lo for x in later)
                    filled=any(x.low<=lo if direction=='LONG' else x.high>=hi for x in later)
                    gaps.append((direction,lo,hi,b.time_ms,{'touch_count':touches,
                        'status':'filled' if filled else 'retested' if touches else 'fresh',
                        'confirmed_ms':b.time_ms+STEPS[tf],'age_ms':now_ms-b.time_ms-STEPS[tf]}))
        for direction,lo,hi,t,meta in gaps[-8:]:add('FVG',tf,t,lo,hi,direction,metadata=meta)
    return items


async def review_allowed(session, now):
    """One normal call plus at most one bounded transient-error retry per slot."""
    from app.models import AiAnalysisRun
    slot=int(now.timestamp()*1000)//14400000*14400000
    rows=(await session.execute(select(AiAnalysisRun).where(AiAnalysisRun.created_at>=now-timedelta(days=1)))).scalars().all()
    attempts=[r for r in rows if ((r.structured or {}).get('analyst_review') or {}).get('slot_ms')==slot]
    if not attempts:return True,slot
    if len(attempts)!=1:return False,slot
    run=attempts[0]
    meta=(run.structured or {}).get('analyst_review') or {}
    from datetime import timezone
    ended=run.completed_at
    if ended and ended.tzinfo is None:ended=ended.replace(tzinfo=timezone.utc)
    usage=((((run.structured or {}).get('_artifacts') or {}).get('inference') or {}).get('response_metadata') or {}).get('usage') or {}
    # Never retry token exhaustion, malformed output, or authentication errors.
    return bool(run.status=='failed' and meta.get('retryable') is True and ended
                and now>=ended+timedelta(minutes=2)
                and int(usage.get('output_tokens') or 0)<5000),slot


def compile_plan(proposal, evidence, series, costs, now_ms, observed_ms):
    if not isinstance(proposal,dict):raise ValueError('invalid_proposal')
    sid=proposal.get('strategy_id');direction=proposal.get('direction')
    if not isinstance(sid,str) or sid not in PROFILES or direction not in ('LONG','SHORT'):raise ValueError('invalid_strategy_direction')
    by_id={x['id']:x for x in evidence}
    try:z,inv,target=(by_id[proposal[k]] for k in ('zone_id','invalidation_id','target_id'))
    except (KeyError,TypeError):raise ValueError('unknown_evidence_id')
    sign=1 if direction=='LONG' else -1
    frames=executable_regime(series)['timeframes'];regime=frames['1h']['trend'];higher=frames['4h']['trend']
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
                     'stop_distance_limit':3*atr(series['15m']),
                     'hourly_regime':regime,'four_hour_regime':higher,'confirmed':False})
    if sid=='ict_reversal':
        from app.services.ict_structure import find_swings
        pivots=[p for p in find_swings(series['5m']) if p.kind==('HIGH' if sign==1 else 'LOW')]
        if not pivots:raise ValueError('missing_reference_swing')
        pivot=pivots[-1]
        c['confirmation_reference']={'liquidity_price':inv['price'], 'liquidity_id':inv['id'],
            'liquidity_timeframe':inv['timeframe'], 'break_price':pivot.price,
            'break_pivot_ms':pivot.time_ms,'break_confirmed_ms':series['5m'][pivot.index+2].time_ms+STEPS['5m']}
    from app.services.analyst_confirmation import recent_confirmations
    c['seed_confirmations']=recent_confirmations(c,series['5m'],observed_ms)
    c['opportunity_id']=f"{VERSION}:{sid}:{direction}:{z['id']}:{observed_ms//14400000}"
    c['candidate_id']=hashlib.sha256(json.dumps(c,sort_keys=True).encode()).hexdigest()[:24]
    c.update(as_of_ms=observed_ms,approved_at_ms=now_ms)
    trigger=TRIGGERS[sid]
    if sid=='ict_reversal':
        ref=c['confirmation_reference']
        trigger=f"Sweep و بازپس‌گیری سطح {ref['liquidity_price']:g}، سپس عبور بسته‌شدن 5m از پیوت مرجع {ref['break_price']:g}؛ ورود با سفارش محدود پس از تأیید کندلی."
    return dict(strategy_id=sid,decision='NO_TRADE',final_ai_decision='WAIT',setup_mode='conditional',
        potential_direction=direction,entry_price=entry,entry_zone_low=z['low'],entry_zone_high=z['high'],
        conditional_entry_zone_low=z['low'],conditional_entry_zone_high=z['high'],stop_loss=stop,take_profit=tp,
        structural_invalidation_level=invalidation,confirmation_type=PROFILES[sid],confirmation_timeframe='5m',
        confirmation_required=trigger,conditional_invalidation='بسته‌شدن 5m آن‌سوی سطح ابطال یا پایان اعتبار چهار‌ساعته.',
        breakout_level=c['breakout_level'],trade_reason=reason,market_regime=regime,market_regime_confidence='low',
        market_context_caution_ack=True,trade_against_dominant_trend=regime==opposed,
        setup_timeframe='intraday_5m_15m',liquidity_event_clear=False,structural_confirmation_clear=False,
        logical_invalidation_clear=True,poi_reaction_clear=False,
        proposed_setup={'new_setup':True,'thesis_fa':reason,'expires_hours':(expiry-now_ms)/3600000},
        verified_trade_plan=c,strategy_candidates={})


def _proposal_diagnostic(proposal, evidence, series, rank):
    """Bounded, candle-derived rejection context; never trust model prices."""
    diagnostic = {"rank": rank}
    if not isinstance(proposal, dict):
        return diagnostic
    for key in ("strategy_id", "direction", "zone_id", "invalidation_id", "target_id"):
        val = proposal.get(key)
        diagnostic[key] = val[:80] if isinstance(val, str) else None
    by_id = {x["id"]: x for x in evidence}
    picked = {key: by_id.get(proposal.get(key)) if isinstance(proposal.get(key), str) else None
              for key in ("zone_id", "invalidation_id", "target_id")}
    for key, value in picked.items():
        diagnostic[key.replace("_id", "_evidence")] = (
            {k: value.get(k) for k in ("id", "kind", "timeframe", "time_ms", "low", "high", "price", "direction", "status")}
            if value else None
        )
    frames = executable_regime(series)["timeframes"]
    diagnostic["regime_1h"] = frames["1h"]["trend"]
    diagnostic["regime_4h"] = frames["4h"]["trend"]
    vol5, vol15 = atr(series["5m"]), atr(series["15m"])
    diagnostic.update(atr5m=vol5, atr15m=vol15, stop_distance_limit=3*vol15)
    zone, invalidation = picked["zone_id"], picked["invalidation_id"]
    direction = proposal.get("direction")
    if zone and invalidation and direction in ("LONG", "SHORT"):
        sign = 1 if direction == "LONG" else -1
        entry = round((zone["low"]+zone["high"])/2, 1)
        reference = invalidation["low"] if sign == 1 else invalidation["high"]
        stop = _tick(reference-sign*max(.25*vol5, .1), sign)
        if sign*(entry-stop) < vol5:
            stop = _tick(entry-sign*vol5, sign)
        diagnostic.update(entry=entry, invalidation_price=reference, stop=stop,
                          stop_distance=sign*(entry-stop),
                          stop_distance_atr15=(round(sign*(entry-stop)/vol15, 3) if vol15 > 0 else None))
    return diagnostic


def compile_response(raw,evidence,series,costs,now_ms,observed_ms,strategies):
    if not isinstance(raw,dict) or not isinstance(raw.get('plans'),list) or len(raw['plans'])>3:
        raise ValueError('invalid_analyst_response')
    accepted={};rejections=[];rejected_plans=[];seen=set()
    for rank,p in enumerate(raw['plans'],1):
        try:
            sid=p.get('strategy_id') if isinstance(p,dict) else None
            if not isinstance(sid,str) or sid not in PROFILES:raise ValueError('invalid_strategy_direction')
            if sid in seen:raise ValueError('duplicate_strategy')
            if not strategies.get(sid):raise ValueError('strategy_disabled')
            seen.add(sid)
            plan=compile_plan(p,evidence,series,costs,now_ms,observed_ms)
            plan['analyst_rank']=rank
            accepted[sid]=plan
        except ValueError as exc:
            reason=str(exc)
            rejections.append(reason)
            item=_proposal_diagnostic(p,evidence,series,rank)
            item['reason']=reason
            rejected_plans.append(item)
    result=next(iter(accepted.values())) if accepted else flat('پیشنهاد قابل اجرای معتبر وجود ندارد.')
    if not accepted:
        # A legacy flat() uses ict_reversal as a default label. An analyst
        # rejection is not an A strategy selection; never attribute one.
        result.pop('strategy_id', None)
    result['strategy_candidates']={sid:p for sid,p in accepted.items() if p is not result}
    forecast,forecast_rejected=compile_forecasts(raw.get('forecast_scenarios',[]),evidence,observed_ms=observed_ms)
    result['analyst_summary']={'market_summary_fa':str(raw.get('market_summary_fa') or '')[:3000],
        'comparison_fa':str(raw.get('comparison_fa') or '')[:3000], 'proposed':len(raw['plans']),
        'accepted':len(accepted),'rejections':rejections,
        'rejected_plans':rejected_plans,
        'forecast_scenarios':forecast,'forecast_rejections':forecast_rejected,
        'execution_regime':executable_regime(series)}
    return result
