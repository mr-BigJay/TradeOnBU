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
from app.services.analyst_regime import executable_regime, continuation_direction
from app.services.analyst_outlook import compile_forecasts
from app.services.analyst_major_levels import select_major_levels, minimum_forecast_gap

VERSION = 'multi_setup_analyst_v1'
PROMPT_VERSION = 12
STEPS = {'1m':60000,'5m':300000,'15m':900000,'1h':3600000,'4h':14400000}
# Signed intraday opportunities may wait for an HTF pullback across six 4H bars.
# The monitor still retires on structural invalidation or target-before-order.
ANALYST_PLAN_TTL_MS = 24 * 60 * 60 * 1000
PROFILES = {'ict_reversal':'htf_sweep_reclaim', 'trend_continuation':'htf_pullback_rejection', 'breakout_retest':'htf_breakout_close'}
TRIGGERS = {
 'ict_reversal':'Sweep و بازپس‌گیری سطح معتبر 1H/4H با یک کندل بستهٔ 5m؛ سفارش Limit برای بازگشت بعدی.',
 'trend_continuation':'سفارش Paper Limit روی ناحیهٔ اصلاح 1H/4H معتبر و هنوز لمس‌نشده؛ Fill فقط با برخورد واقعی قیمت بعد از ثبت سفارش. واکنش 5m برای اجرای Limit اولیه اجباری نیست.',
 'breakout_retest':'شکست قطعی حمایت/مقاومت 1H/4H با بسته‌شدن کندل ۱۵ یا ۵ دقیقه بیرون ناحیه؛ سفارش Limit روی بازآزمایی همان سطح.',
}
INSTRUCTIONS = '''You are a multi-scenario BTCUSDT analyst, not a reviewer of preselected reversal candidates.
Compare trend continuation, reversal, and breakout/retest using 4H/1H context and 15m/5m execution. Prefer trend-continuation pullbacks, then breakout opportunities, then countertrend reversals where evidence supports them; NEVER manufacture a setup to meet a quota.
Use the provided closed candles, evidence_catalog, strategy_features, execution_regime and freshness-filtered market_intelligence.
The market.timeframes[*].candles rows use the explicit candles_columns order [time_ms,open,high,low,close,volume], with UTC epoch milliseconds. All 5m/15m/1h/4h bars are present; 1m includes the latest 45 closed minutes for sequencing only. time_iso is derivable from time_ms.
EMA9/20, RSI14, ATR, ADX and relative volume are descriptive context for comparing scenarios,
NEVER mandatory entry thresholds. RSI extremes or EMA crosses alone are not permission to trade.
Return ONLY JSON with market_summary_fa, comparison_fa, forecast_scenarios, and plans.
The four-hour outlook must explain only evidence-backed paths conditionally, NOT claim guaranteed movements.
outlook_major_levels contains ONLY independently screened major 1H/4H candle-derived
support/resistance or confirmed displacement order blocks. For forecast_scenarios,
choose level_id and target_id ONLY from outlook_major_levels. Do NOT use the
5m/15m catalog for outlook levels or destinations; it remains available ONLY for
5m/15m executable plan precision. If the major list has too few defensible levels,
return zero or fewer scenarios rather than forcing a count.
The four-hour outlook must explain the likely paths *conditionally*, NOT claim guaranteed price movements:
support rebound on a fresh/untested zone, resistance rejection, tested support breakdown with retest,
resistance breakout with retest, or pullback continuation, wherever actual candle evidence supports them.
Compare what happens IF price reaches a zone, IF it holds, and IF it breaks; include 1h/4h structural
trend, EMA9/20 direction, RSI14, ADX14, ATR14 and volume when available and relevant.
A previously tested area is NOT necessarily going to break: describe test count, displacement,
rejection strength and volume as context, not a quantified probability or a signal by themselves.
forecast_scenarios is 0 to 4 ranked watch-only objects. Never fill a quota with weak levels.
A target must be fully outside the source zone with meaningful ATR-scaled room,
and no nearer major structural obstacle. Selection is server validated.
Each: scenario_type (support_bounce|resistance_rejection|support_break|resistance_break|trend_pullback),
direction (LONG|SHORT), level_id (exact evidence_catalog ID), target_id (exact evidence_catalog ID or null),
reason_fa (why this area is significant, considering touch_count, status, HTF/LTF and relevant indicators).
Do not invent levels. The server resolves every forecast price, state, condition and invalidation from evidence.
All forecast_scenarios are a *watchlist*, NOT executable positions. Provide forward-looking levels even
when no executable plan passes the server rules. Never promise that an untouched area must reverse.
plans is 0 to 3 *independently executable conditional proposals* ranked best first.
Each plan: strategy_id (ict_reversal|trend_continuation|breakout_retest), direction (LONG|SHORT),
zone_id, invalidation_id, target_id (exact supplied catalog IDs), reason_fa.
For C choose the broken boundary as the entry and the latest confirmed opposite swing beyond the entire entry zone as invalidation. Prefer an established HTF zone with independent reactions. A/B may use their own confirmed zone edge as a
structural stop. This is not an arbitrary or micro-candle stop. The same
strict net 1.5R, nearest intervening HTF obstacle, stop-width safety and
position-risk sizing remain in force. Favor the closest defensible structural
invalidation over a distant unrelated 4H swing. Never tighten the stop to
manufacture R:R or treat another zone's remote high/low as obligatory.
At most one plan per strategy. Only enabled strategies. Empty plans is valid when no structurally sound, fee-adjusted conditional setup exists. A and C may propose a monitorable conditional plan BEFORE their future closed-5m sweep/breakout trigger; the monitor, not the AI, will verify the trigger before submitting a limit. B may also place a resting signed HTF pullback LIMIT early. Do not force market orders or fabricate evidence.
Select meaningful candle-backed areas; NEVER invent price levels, IDs, confidence probabilities or confirmations. Executable support/resistance and OB source zones MUST come from 1H or 4H; 5m/15m evidence is for FVG execution precision and candle confirmation, never an arbitrary micro support/resistance trade. Prefer stops supported by structural invalidation, not micro swings.
For continuation, also compare fresh directional 5m/15m FVG pullbacks on the resting side of price with a separately confirmed HTF invalidation and structural target. Prefer the nearest executable pullback. For continuation, 4H confirmed swing structure determines the direction: bearish => SHORT on a rally/pullback into HTF resistance/OB; bullish => LONG on a decline/pullback into HTF support/OB. Opposing 1H structure can be the correction, NOT an automatic veto. If 4H is range/unclear, use directional 1H structure. For continuation, a signed and server-validated HTF pullback zone may receive a resting paper LIMIT before a 5m directional rejection, only if price has not already touched or invalidated it. A fill still requires a later independent exchange 1m touch after order creation. This is not an immediate market entry. EMA/RSI are context, not mandatory thresholds. Breakout uses resistance for LONG or support for SHORT.
execution_feasibility contains server-checked geometry choices using actual paper costs and the nearest structural obstacle. It also includes zone_gap_atr15 and live_to_target_atr15: compare travel needed to reach an entry zone against the remaining room to the target. When trend-continuation B is far from current price while the target is close, do not repeatedly select an implausible B pullback; assess a valid same-direction breakout/retest C instead. For a structurally sound B LIMIT proposal, do NOT require the current price to already touch the HTF zone or the 5m candle to already reject it: the order is intentionally posted in advance. For A/C conditional plans, the reversal sweep or breakout close is a future MONITOR trigger, not an AI proposal prerequisite. Review execution_feasibility.choices first: each has already passed compiler geometry, trend alignment, historical level confirmation and minimum net R:R. Prefer such nearby, still reachable B proposals when justified; do not invent IDs or turn watch-only forecasts into positions. If feasible choices are deliberately omitted, give a specific qualitative objection beyond mere distance or absent immediate 5m confirmation. This is advisory, not a new eligibility gate. Use these to avoid impossible entry/stop/target combinations, then assess market quality yourself. They are NOT confirmed signals or mandatory selections. You may propose other catalog combinations; all pass the same final validation. If no plan is selected, explain whether geometry or market evidence is the reason. Never invent a plan to meet a quota.
Use market.chart_patterns as descriptive evidence for A/B/C even when no plan is selected. Compare overlapping larger and smaller structures, particularly a rising wedge inside a descending channel. Mention relevant timeframe, boundary and price position; do not infer a confirmed breakout or authorize an order from a pattern label.
Use previous_review to compare the prior primary scenario with closed candles in its own validity window. State which level was reached and which condition remains open in market_summary_fa. Describe the expected sequence of moves. Check converging triangle edges and trendlines for C and describe their anchors; use catalog trendline boundaries with signed slope and anchor times. Their fixed paper limit expires before it leaves the projected band.
Explain why each selected scenario is preferable and why alternatives were not selected.
Server derives midpoint limit entry, ATR-buffered structural stop and cost-adjusted target before the nearest obstacle.
Server checks one objective closed-5m trigger for promotion to READY: reversal=HTF liquidity sweep and strong reclaim; continuation=directional rejection at selected HTF pullback area; breakout=5m close crossing a HTF level. Signed continuation plans may additionally rest a LIMIT immediately on a still-fresh, untouched HTF zone with all original structural SL, risk and net R:R checks; no fill is assumed until an independent post-order exchange 1m touch. A/C retain their normal confirmation gates. Do not claim an additional MSS, retest confirmation or 5m micro support is mandatory for the new analyst contract. Legacy setups retain their own historical confirmation policies.
A proposal is a conditional execution candidate, NOT an already opened position. Server-verified B LIMIT orders may rest before a 5m signal, while a future actual exchange touch is required for fill. A and C keep their closed-5m confirmations. Never claim a pre-order touch or fill, and never synthesize market-on-touch orders.
Respect source/timeframe/venue caveats; missing optional evidence is not a directional signal. No return promises.
Start market_summary_fa with one complete Persian sentence under 180 characters describing the expected near-term conditional path. Put indicator details in subsequent sentences.
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
    # A just-broken hourly swing disappears from the ordinary unbroken-level catalog.
    # Rebuild the levels known before the previous four-hour review window for C only.
    cutoff = now_ms//STEPS["5m"]*STEPS["5m"] - STEPS["4h"]
    historical = {tf: [b for b in series[tf] if b.time_ms+STEPS[tf] <= cutoff] for tf in ("1h","4h")}
    present = {(x["timeframe"], x["kind"], x["time_ms"]) for x in items}
    for level in market_zones(historical)["support_resistance"]:
        tf, role, stamp = level["timeframe"], level["kind"], level["time_ms"]
        if (tf,role,stamp) in present:
            continue
        price = level["price"]
        width = max(.15*atr(historical[tf]), .1)
        direction = "LONG" if role == "resistance" else "SHORT"
        add("recent_break",tf,stamp,price-width,price+width,direction,price,
            {"confirmed_ms":stamp+3*STEPS[tf],"original_kind":role})
    from app.services.breakout_boundaries import boundaries
    for tf in ("1h", "4h"):
        width = max(.15*atr(series[tf]), .1)
        for direction, line in boundaries(series[tf], STEPS[tf], now_ms, width).items():
            price = line["price"]
            add("trendline", tf, line["time_ms"], price-width, price+width, direction, price, line)
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
    slot=int(now.timestamp()*1000)//STEPS['4h']*STEPS['4h']  # One paid review per 4H slot
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


def fresh_pullback_gap(zone, direction, live):
    """A confirmed untested imbalance entirely on the pullback side of price."""
    return (zone["kind"] == "FVG" and zone["timeframe"] in ("5m", "15m")
            and zone.get("status") == "fresh" and zone.get("confirmed_ms") is not None
            and zone.get("direction") == direction
            and (live > zone["high"] if direction == "LONG" else live < zone["low"]))


def breakout_stop_anchor(zone, evidence, direction):
    long = direction == "LONG"
    kind = "support" if long else "resistance"
    swings = [x for x in evidence if x["kind"] == kind and x["timeframe"] in ("15m", "1h", "4h")
              and (x["price"] < zone["low"] if long else x["price"] > zone["high"])]
    return max(swings, key=lambda x: x["time_ms"], default=None)


def proposal_geometry(direction, zone, invalidation, target, evidence, series, costs=None):
    """One price/risk calculation for compilation, preflight and diagnostics."""
    from app.domain.paper_execution_policy import HARD_MIN_AUTO_PAPER_NET_RR
    from app.services.execution_model_sim import auto_paper_rr_from_settings
    sign = 1 if direction == "LONG" else -1
    vol, vol15, vol1h = (atr(series[tf]) for tf in ("5m", "15m", "1h"))
    entry = round((zone["low"] + zone["high"]) / 2, 1)
    live = float(series["1m"][-1].close)
    zone_gap = max(float(zone["low"]) - live, live - float(zone["high"]), 0.0)
    reference = invalidation["low"] if sign == 1 else invalidation["high"]
    # A confirmed 1H/4H zone can itself supply structural invalidation at its
    # distal edge. An unrelated remote swing isn't required as a stop anchor.
    # Keep an explicit ATR-scaled minimum stop distance to prevent tiny stops.
    buffer = max(.25 * vol, .1)
    minimum_stop_distance = max(vol, .5 * vol15)
    stop = _tick(reference - sign * buffer, sign)
    if sign * (entry - stop) < minimum_stop_distance:
        stop = _tick(entry - sign * minimum_stop_distance, sign)
    source = ("zone_distal_edge" if invalidation["id"] == zone["id"]
              else "separate_confirmed_level")
    result = dict(entry=entry, invalidation_price=reference, stop=stop,
                  invalidation_source=source, stop_atr_buffer=buffer,
                  minimum_stop_distance=minimum_stop_distance,
                  atr5m=vol, atr15m=vol15, atr1h=vol1h,
                  stop_distance_limit=max(3 * vol15, vol1h),
                  stop_distance=sign * (entry-stop),
                  stop_distance_atr15=round(sign*(entry-stop)/vol15, 3) if vol15 else None,
                  zone_gap_atr15=round(zone_gap/vol15, 3) if vol15 else None,
                  required_min_net_rr=HARD_MIN_AUTO_PAPER_NET_RR)
    if target is None:
        return result
    obstacles = [target] + [x for x in evidence if x["timeframe"] in ("1h", "4h")
        and x["kind"] == ("resistance" if sign == 1 else "support")
        and sign * (x["price"]-entry) > .1*vol]
    nearest = min(obstacles, key=lambda x: sign*x["price"])
    obstacle = nearest["price"] - sign * .1*vol
    result.update(obstacle=obstacle, obstacle_level_id=nearest["id"],
                  selected_target_price=target["price"])
    if costs is not None and sign*(obstacle-entry) > 0 and sign*(entry-stop) > 0:
        rr = auto_paper_rr_from_settings(costs, direction=direction, entry=entry,
            sl=stop, tp=obstacle, min_net_rr=HARD_MIN_AUTO_PAPER_NET_RR)
        result["obstacle_net_rr"] = rr.net_rr
        result["target"] = target_at_net_rr(costs, direction, entry, stop, obstacle)
        if result["target"] is not None and vol15:
            result["live_to_target_atr15"] = round(sign*(result["target"]-live)/vol15, 3)
    return result


def compile_plan(proposal, evidence, series, costs, now_ms, observed_ms, *, seed=True):
    if not isinstance(proposal,dict):raise ValueError('invalid_proposal')
    sid=proposal.get('strategy_id');direction=proposal.get('direction')
    if not isinstance(sid,str) or sid not in PROFILES or direction not in ('LONG','SHORT'):raise ValueError('invalid_strategy_direction')
    by_id={x['id']:x for x in evidence}
    try:z,inv,target=(by_id[proposal[k]] for k in ('zone_id','invalidation_id','target_id'))
    except (KeyError,TypeError):raise ValueError('unknown_evidence_id')
    sign=1 if direction=='LONG' else -1
    frames=executable_regime(series)['timeframes'];regime=frames['1h']['trend'];higher=frames['4h']['trend']
    aligned='bullish' if sign==1 else 'bearish';opposed='bearish' if sign==1 else 'bullish'
    if sid=='trend_continuation' and continuation_direction(frames)!=direction:raise ValueError('continuation_trend_conflict')
    if sid=='breakout_retest' and z['kind'] not in ('trendline', 'recent_break', 'resistance' if sign==1 else 'support'):raise ValueError('breakout_requires_level')
    shallow = sid == 'trend_continuation' and fresh_pullback_gap(z, direction, series['1m'][-1].close)
    if not shallow:
        if sid in ('ict_reversal','trend_continuation') and z['kind'] not in (('support' if sign==1 else 'resistance'),'OB'):
            raise ValueError('reversal_continuation_requires_support_resistance')
        if z['timeframe'] not in ('1h', '4h') or z['kind'] not in ('support', 'resistance', 'OB', 'trendline', 'recent_break'):
            raise ValueError('execution_requires_major_structure')
    if sid == 'breakout_retest':
        anchor = breakout_stop_anchor(z, evidence, direction)
        if anchor is None:
            raise ValueError('breakout_requires_opposite_swing')
        inv = anchor
    if inv['timeframe'] not in (('15m','1h','4h') if sid=='breakout_retest' else ('1h','4h')) or inv['kind'] not in ('support', 'resistance', 'OB'):
        raise ValueError('stop_requires_major_structure')
    same_zone_invalidation = z['id'] == inv['id']
    if not same_zone_invalidation and inv['kind'] in ('support','resistance') and inv['kind'] != ('support' if sign==1 else 'resistance'):
        raise ValueError('invalidation_level_wrong_side')
    # A level is not eligible until its confirming candles have closed.
    for item in (z, inv, target):
        confirmed = item.get('confirmed_ms')
        if confirmed is None and item.get('kind') in ('support','resistance'):
            confirmed = item['time_ms'] + 3 * STEPS[item['timeframe']]
        if confirmed is not None and (not isinstance(confirmed, int) or confirmed > observed_ms):
            raise ValueError('major_structure_not_yet_confirmed')
    if z.get('direction') and z['direction']!=direction:raise ValueError('zone_direction_conflict')
    entry=round((z['low']+z['high'])/2,1)
    invalidation=inv['low'] if sign==1 else inv['high']
    if not same_zone_invalidation and sign*((z['low'] if sign==1 else z['high'])-invalidation)<=0:
        raise ValueError('invalidation_inside_zone')
    if sign*(series['1m'][-1].close-invalidation)<=0:raise ValueError('already_invalidated')
    if target['kind']!=('resistance' if sign==1 else 'support') or target['timeframe'] not in ('1h','4h'):raise ValueError('target_requires_structural_level')
    geometry = proposal_geometry(direction, z, inv, target, evidence, series, costs)
    vol, stop = geometry["atr5m"], geometry["stop"]
    stop_limit = geometry["stop_distance_limit"]
    if not 0 < geometry["stop_distance"] <= stop_limit:raise ValueError('stop_too_wide')
    obstacle = geometry["obstacle"]
    if sign*(obstacle-entry)<=0:raise ValueError('target_wrong_side')
    tp = geometry.get("target")
    if tp is None:raise ValueError('insufficient_net_reward_before_obstacle')
    if sign*(series['1m'][-1].close-tp)>=0:raise ValueError('target_already_reached')
    expiry=observed_ms+ANALYST_PLAN_TTL_MS
    if z.get("valid_until_ms"):
        expiry = min(expiry, z["valid_until_ms"])
    if now_ms>=expiry:raise ValueError('proposal_expired')
    reason=str(proposal.get('reason_fa') or '')[:1600]
    if not reason.strip():raise ValueError('missing_reason')
    c=dict(version=VERSION,strategy_id=sid,confirmation_type=PROFILES[sid],direction=direction,
           entry=entry,zone=[z['low'],z['high']],stop=stop,target=tp,invalidation=invalidation,
           formed_ms=observed_ms,expires_ms=expiry,breakout_level=z['price'] if sid=='breakout_retest' else None,
           evidence={'zone':z,'invalidation':inv,'target':target,'atr14':vol,'obstacle':obstacle,
                     'stop_distance_limit':stop_limit,
                     'invalidation_source':geometry['invalidation_source'],
                     'stop_atr_buffer':geometry['stop_atr_buffer'],
                     'minimum_stop_distance':geometry['minimum_stop_distance'],
                     'hourly_regime':regime,'four_hour_regime':higher,'confirmed':False})
    if sid == 'breakout_retest':
        c['breakout_zone_confirmation'] = True
        c['boundary_known_ms'] = z.get('confirmed_ms', z['time_ms']+3*STEPS[z['timeframe']])
    if sid=='ict_reversal':
        # A single *closed* sweep/reclaim of the server-supplied major
        # invalidation level replaces the old mandatory sweep-plus-MSS chain.
        # For the zone's own distal edge, prove a real sweep through that
        # structural boundary, not merely a wick through its midpoint.
        liquidity_price = (invalidation if same_zone_invalidation else inv['price'])
        c['confirmation_reference']={'liquidity_price':liquidity_price,
            'liquidity_id':inv['id'], 'liquidity_timeframe':inv['timeframe']}
    from app.services.analyst_confirmation import recent_confirmations
    c['seed_confirmations']=recent_confirmations(c,series['5m'],observed_ms) if seed else {}
    if seed and z['kind'] == 'recent_break' and not c['seed_confirmations']:
        raise ValueError('breakout_retest_already_spent_or_unconfirmed')
    c['opportunity_id']=f"{VERSION}:{sid}:{direction}:{z['id']}:{observed_ms//14400000}"
    c['candidate_id']=hashlib.sha256(json.dumps(c,sort_keys=True).encode()).hexdigest()[:24]
    c.update(as_of_ms=observed_ms,approved_at_ms=now_ms)
    trade_regime = higher if sid == 'trend_continuation' and higher in ('bullish', 'bearish') else regime
    trigger=TRIGGERS[sid]
    if shallow:
        trigger = f"سفارش Limit روی اصلاح در FVG تایم {z['timeframe']} با حد ضرر ساختاری 1H/4H؛ ورود پس از لمس قیمت سفارش."
    if sid=='ict_reversal':
        ref=c['confirmation_reference']
        trigger=f"Sweep و بازپس‌گیری معتبر سطح {ref['liquidity_price']:g} با کندل بستهٔ 5m؛ ثبت سفارش Limit برای پولبک بعدی."
    return dict(strategy_id=sid,decision='NO_TRADE',final_ai_decision='WAIT',setup_mode='conditional',
        potential_direction=direction,entry_price=entry,entry_zone_low=z['low'],entry_zone_high=z['high'],
        conditional_entry_zone_low=z['low'],conditional_entry_zone_high=z['high'],stop_loss=stop,take_profit=tp,
        structural_invalidation_level=invalidation,confirmation_type=PROFILES[sid],confirmation_timeframe='5m',
        confirmation_required=trigger,conditional_invalidation='بسته‌شدن 5m آن‌سوی سطح ابطال، رسیدن به هدف پیش از سفارش یا پایان اعتبار ۲۴ساعته.',
        breakout_level=c['breakout_level'],trade_reason=reason,market_regime=trade_regime,market_regime_confidence='low',
        market_context_caution_ack=True,trade_against_dominant_trend=trade_regime==opposed,
        setup_timeframe='intraday_5m_15m',liquidity_event_clear=False,structural_confirmation_clear=False,
        logical_invalidation_clear=True,poi_reaction_clear=False,
        proposed_setup={'new_setup':True,'thesis_fa':reason,'expires_hours':(expiry-now_ms)/3600000},
        verified_trade_plan=c,strategy_candidates={})


def _proposal_diagnostic(proposal, evidence, series, rank, costs=None):
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
    zone, invalidation = picked["zone_id"], picked["invalidation_id"]
    direction = proposal.get("direction")
    if zone and invalidation and direction in ("LONG", "SHORT"):
        diagnostic.update(proposal_geometry(direction, zone, invalidation,
            picked["target_id"], evidence, series, costs))
    return diagnostic


def compile_response(raw,evidence,series,costs,now_ms,observed_ms,strategies,*,outlook_major_levels=None,execution_feasibility=None):
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
            item=_proposal_diagnostic(p,evidence,series,rank,costs)
            item['reason']=reason
            rejected_plans.append(item)
    # The model may describe valid future setups but return plans=[] because
    # their 5m trigger hasn't yet happened. Recover per missing strategy from
    # already server-feasible geometry, while NEVER fabricating confirmations.
    # A/C remain WAIT until real future 5m triggers; B can rest an HTF limit.
    ai_accepted_count = len(accepted)
    fallback_audits = []
    hints = (execution_feasibility or {}).get('choices') or []
    live = float(series['1m'][-1].close)
    from math import isfinite

    def _fallback_options(sid):
        options = []
        for item in hints:
            if not isinstance(item, dict) or item.get('strategy_id') != sid:
                continue
            direction = item.get('direction')
            if direction not in ('LONG', 'SHORT'):
                continue
            sign = 1 if direction == 'LONG' else -1
            try:
                entry, gap, room, stop_distance = (
                    float(item[key]) for key in (
                        'entry', 'zone_gap_atr15', 'live_to_target_atr15',
                        'stop_distance',
                    )
                )
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if not all(isfinite(v) for v in (entry, gap, room, stop_distance)):
                continue
            # A marketable or already-spent B LIMIT is not executable in our
            # forward-only pending-order model. For C, require the opposite:
            # price must be BEFORE the structural breakout level.
            if sid == 'trend_continuation' and sign * (live - entry) <= 0:
                continue
            if sid == 'breakout_retest' and sign * (live - entry) >= 0 and not item.get('recent_break'):
                continue
            # A is conditional reversal evidence, NOT a resting pre-order;
            # monitor waits for a fresh sweep/reclaim before any limit.
            if not 0 <= gap <= 2.5 or room <= 0.5:
                continue
            options.append((gap, -room, stop_distance, item))
        return sorted(options, key=lambda x: (x[0], x[1], x[2]))

    # Preserve AI's choices and rank. Avoid replacing a malformed AI proposal
    # with a fallback for the *same* strategy: record its rejection instead.
    # Distinct strategies may be monitored independently; portfolio-wide
    # position and loss caps still decide whether their orders may execute.
    for sid in ('trend_continuation', 'breakout_retest', 'ict_reversal'):
        if sid in seen or sid in accepted or not strategies.get(sid):
            continue
        for _, _, _, item in _fallback_options(sid):
            suggestion = {k: item[k] for k in (
                'strategy_id', 'direction', 'zone_id',
                'invalidation_id', 'target_id'
            ) if k in item}
            condition = {
                'trend_continuation': 'پولبک به ناحیهٔ ساختاری روند',
                'breakout_retest': 'شکست معتبر سطح و بازآزمایی بعدی',
                'ict_reversal': 'جاروب نقدینگی و بازپس‌گیری معتبر سطح',
            }[sid]
            suggestion['reason_fa'] = (
                'پلن مشروط آزمایشی Paper از هندسهٔ تأییدشدهٔ سرور برای '
                + condition + '؛ هیچ تأیید ورود یا معاملهٔ انجام‌شده‌ای فرض نشده است.'
            )
            try:
                plan = compile_plan(suggestion, evidence, series, costs,
                                    now_ms, observed_ms)
            except ValueError:
                continue
            plan['analyst_rank'] = len(accepted) + 1
            accepted[sid] = plan
            fallback_audits.append(dict(
                source='server_compiled_paper_conditional',
                strategy_id=sid, direction=item['direction'],
                zone_id=item['zone_id'],
                zone_gap_atr15=item['zone_gap_atr15'],
                ai_proposed=False,
                order_policy=('preorder_resting_limit' if sid == 'trend_continuation'
                              else 'wait_for_real_closed_5m_trigger'),
            ))
            break
    fallback_audit = fallback_audits[0] if fallback_audits else None
    result=next(iter(accepted.values())) if accepted else flat('پیشنهاد قابل اجرای معتبر وجود ندارد.')
    if not accepted:
        # A legacy flat() uses ict_reversal as a default label. An analyst
        # rejection is not an A strategy selection; never attribute one.
        result.pop('strategy_id', None)
    result['strategy_candidates']={sid:p for sid,p in accepted.items() if p is not result}
    major_levels = (select_major_levels(evidence,series) if outlook_major_levels is None
                    else outlook_major_levels)
    forecast,forecast_rejected=compile_forecasts(
        raw.get('forecast_scenarios',[]),evidence,observed_ms=observed_ms,
        major_levels=major_levels,min_gap=minimum_forecast_gap(series))
    result['analyst_summary']={'market_summary_fa':str(raw.get('market_summary_fa') or '')[:3000],
        'comparison_fa':str(raw.get('comparison_fa') or '')[:3000], 'proposed':len(raw['plans']),
        'accepted':len(accepted),'rejections':rejections,
        'rejected_plans':rejected_plans,
        'forecast_scenarios':forecast,'forecast_rejections':forecast_rejected,
        'outlook_major_levels':major_levels,'outlook_major_policy':'major_htf_levels_v1',
        'execution_regime':executable_regime(series),
        'execution_fallback':fallback_audit,
        'execution_fallbacks':fallback_audits,
        'ai_accepted':ai_accepted_count,
        'fallback_accepted':len(fallback_audits)}
    return result
