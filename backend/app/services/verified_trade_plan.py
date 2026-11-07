"""Versioned, candle-derived BTC plans. The model can veto, never set prices.

All inputs are closed bars available at as_of_ms. No optimised parameters or
claims of profitability: this is a falsifiable paper-trading baseline.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from copy import deepcopy
from datetime import timezone

from app.domain.candle_quality import acceptable_prices, displacement_body
from app.services.ict_structure import find_swings
from app.services.ict_execution_plan import target_at_net_rr

REASONS_FA = {
 'verified_plan_missing':'سناریوی قدیمی؛ قرارداد کندلی جدید ندارد.',
 'verified_plan_invalid':'قرارداد کندلی ناقص است.',
 'verified_plan_expired':'مهلت فرصت کندلی پایان یافته است.',
 'verified_plan_levels_changed':'سطوح سناریو با قرارداد تأییدشده یکسان نیست.',
 'verified_plan_contract_changed':'شواهد یا سطوح قرارداد تغییر کرده است.',
 'verified_market_data_invalid':'کندل‌های تازه سالم و پیوسته نیستند.',
 'verified_freshness_unavailable':'پوشش یک‌دقیقه‌ای برای بررسی تازگی کافی نیست.',
 'verified_pullback_already_used':'قیمت پیش از ثبت سفارش به قیمت ورود رسیده؛ فرصت مصرف شده است.',
 'verified_one_position_limit':'یک پوزیشن یا سفارش فعال وجود دارد.',
 'verified_daily_loss_limit':'سقف زیان روزانه رسیده؛ ورود تا روز بعد متوقف است.',
 'verified_no_risk_budget':'بودجهٔ ریسک یا موجودی آزاد کافی نیست.',
}
VERSION = 'verified_reversal_sequence_v3'
STEP = 300_000
MAX_AGE = 6 * STEP
TTL = 12 * STEP
REVIEW_INSTRUCTIONS = '''You review mechanically verified BTCUSDT reversal candidates.
Return ONLY JSON: {"candidate_id": "one supplied id or null", "approve": true/false,
"reason_fa": "brief Persian explanation", "risk_fa": "brief opposing evidence"}.
Select at most one candidate. Reject if higher-timeframe context conflicts or
available evidence is insufficient. You may NEVER invent a candidate, price,
confirmation, confidence probability, or promised return. Prices, stop, target,
expiry and position sizing belong to the server. No candidate is a valid outcome.
Use only provided closed candles and candidate evidence. Prior reports are not
new evidence. Assess structural coherence and remaining room to target. Use market.poi_context to judge nearby order blocks and support/resistance. These candle-derived zones do not prove institutional orders. Prefer a coherent reaction at a relevant area; never approve merely because price touches a zone.
market_intelligence is optional, freshness-filtered context. Preserve its source,
exchange and sampling caveats; absence is not a bearish or bullish observation.
An opposing 1H trend is explicitly flagged with a confirmed 15m reversal break;
weigh that evidence rather than assuming the old 1H direction can never reverse.
'''


def enabled():
    return os.getenv('VERIFIED_PLANS_ENABLED', '0') == '1' or os.getenv('MULTI_SETUP_ANALYST_ENABLED','0') == '1'


def valid_bars(bars, step, as_of_ms, minimum=20):
    if len(bars) < minimum:
        return False
    for i, b in enumerate(bars):
        vals = (b.open, b.high, b.low, b.close)
        if not all(isinstance(x, (int, float)) and math.isfinite(x) and x > 0 for x in vals):
            return False
        if not acceptable_prices(b, bars[i-1] if i else None, step):
            return False
        if not isinstance(b.time_ms, int) or b.time_ms % step or b.time_ms + step > as_of_ms:
            return False
    return (all(b.time_ms - a.time_ms == step for a, b in zip(bars, bars[1:]))
            and bars[-1].time_ms == as_of_ms // step * step - step)


def atr(bars, period=14):
    ranges = [max(b.high-b.low, abs(b.high-a.close), abs(b.low-a.close))
              for a, b in zip(bars, bars[1:])]
    return sum(ranges[-period:]) / period if len(ranges) >= period else 0.0


def trend(bars):
    pivots = find_swings(bars)
    highs = [p.price for p in pivots if p.kind == 'HIGH']
    lows = [p.price for p in pivots if p.kind == 'LOW']
    if min(len(highs), len(lows)) < 2:
        return 'unclear'
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return 'bullish'
    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return 'bearish'
    return 'range'


def _tick(value, sign):
    return (math.floor(value * 10) if sign > 0 else math.ceil(value * 10)) / 10


def same_bar_sequence(minutes, bar, ref, pivot, sign):
    """A 5m OHLC cannot establish event order. Require all five 1m bars,
    with the first structural close strictly after the first liquidity sweep.
    """
    rows = [x for x in minutes if bar.time_ms <= x.time_ms < bar.time_ms+STEP]
    if [x.time_ms for x in rows] != list(range(bar.time_ms, bar.time_ms+STEP, 60_000)):
        return False
    if any(not math.isclose(a,b,rel_tol=1e-10,abs_tol=1e-8) for a,b in
           ((max(x.high for x in rows),bar.high),(min(x.low for x in rows),bar.low),(rows[-1].close,bar.close))):
        return False
    swept = next((i for i,x in enumerate(rows)
                  if sign*(ref-(x.low if sign == 1 else x.high)) > 0), None)
    broken = next((i for i,x in enumerate(rows) if sign*(x.close-pivot)>0), None)
    return swept is not None and broken is not None and swept < broken


def countertrend_break(bars, sweep_ms, sign):
    """Countertrend permission requires a new displaced 15m swing break,
    after the 5m sweep; old 1H pivots alone must not veto a proven reversal.
    """
    known = [p for p in find_swings(bars) if p.time_ms+3*3*STEP <= sweep_ms
             and p.kind == ('HIGH' if sign == 1 else 'LOW')]
    if not known:
        return None
    pivot = known[-1]
    for i in range(pivot.index+1, len(bars)):
        b = bars[i]
        if sign*(b.close-pivot.price) <= 0:
            continue
        # Only the FIRST close through the known pivot is a new break.
        if b.time_ms < sweep_ms:
            return None
        vol = atr(bars[:i])
        if vol > 0 and displacement_body(b, sign) >= max(vol, .6*(b.high-b.low)):
            return {'time_ms': b.time_ms, 'pivot_ms': pivot.time_ms, 'price': pivot.price}
        return None
    return None


def build_candidates(series, costs, as_of_ms, live, *, audit=None):
    """Sweep of a known pivot, opposite-swing close break with displacement,
    fresh FVG or order block, ATR-buffered invalidation, net 1.4R before obstacles.
    Return candidates + explicit rejection counters (no LLM calls).
    """
    diagnostics = {}
    direction = None
    sweep = None
    b = ref = pivot = None
    if audit is not None:
        audit.clear()
        audit.update(version=1, as_of_ms=as_of_ms, live_price=live,
                     candidate_count=0,
                     count_unit="branch events, not distinct opportunities", directions={},
                     input_counts={tf:len(rows) for tf,rows in series.items()})
    def stage(name):
        if audit is not None and direction:
            counts = audit['directions'][direction]['stages']
            counts[name] = counts.get(name, 0) + 1
    def reject(reason):
        diagnostics[reason] = diagnostics.get(reason, 0) + 1
        if audit is not None and direction:
            detail = audit['directions'][direction]
            detail['reasons'][reason] = detail['reasons'].get(reason, 0) + 1
            samples = detail['samples'].setdefault(reason, [])
            if len(samples) < 3:
                samples.append({'sweep_ms': sweep.time_ms if sweep else None,
                                'break_ms': b.time_ms if b else None,
                                'swept_price': ref.price if ref else None,
                                'broken_price': pivot.price if pivot else None,
                                'sweep_ohlc': [sweep.open,sweep.high,sweep.low,sweep.close] if sweep else None,
                                'break_ohlc': [b.open,b.high,b.low,b.close] if b else None})
    def data_failure(reason):
        if audit is not None:
            audit.update(status='data_blocked', rejection_counts={reason:1})
        return [], {reason:1}
    for tf, step in (('5m', STEP), ('15m', 3*STEP), ('1h', 12*STEP)):
        if not valid_bars(series.get(tf, []), step, as_of_ms, 30):
            return data_failure('invalid_or_stale_'+tf)
    if '4h' in series and not valid_bars(series['4h'], 48*STEP, as_of_ms, 20):
        return data_failure('invalid_or_stale_4h')
    if '1m' in series and not valid_bars(series['1m'], 60_000, as_of_ms, 20):
        return data_failure('invalid_or_stale_1m')
    if not isinstance(live, (int, float)) or not math.isfinite(live) or live <= 0:
        return data_failure('invalid_quote')
    bars = series['5m']; pivots = find_swings(bars); vol = atr(bars)
    regime = trend(series['1h'])
    from app.services.poi_context import market_zones
    zones = market_zones(series)
    candidates = []
    if audit is not None:
        audit['hourly_regime'] = regime
        hp = find_swings(series['1h'])
        audit['hourly_pivots'] = {kind:[{'time_ms':p.time_ms,'price':p.price}
            for p in hp if p.kind == kind][-2:] for kind in ('HIGH','LOW')}
    for direction, sign in (('LONG', 1), ('SHORT', -1)):
        sweep = b = ref = pivot = None
        if audit is not None:
            audit['directions'][direction] = {'status':'scanned','stages':{},'reasons':{},'samples':{}}
        countertrend = regime == ('bearish' if sign == 1 else 'bullish')
        for si in range(max(16, len(bars)-18), len(bars)-1):
            sweep = bars[si]
            b = ref = pivot = None
            stage('sweep_bars_examined')
            known = [p for p in pivots if p.index+2 < si]
            swept = [p for p in known if p.kind == ('LOW' if sign==1 else 'HIGH')]
            broken = [p for p in known if p.kind == ('HIGH' if sign==1 else 'LOW')]
            if not swept or not broken:
                reject('missing_reference_swings'); continue
            pivot = broken[-1]
            extreme = sweep.low if sign==1 else sweep.high
            crossed = [p for p in swept if sign*(p.price-extreme)>0]
            if not crossed:
                ref = swept[-1]
                reject('no_liquidity_sweep'); continue
            # The latest internal swing is not necessarily the liquidity pool
            # being swept. Select the most recent *untouched crossed* reference.
            fresh = [p for p in crossed if not any(
                (x.low<p.price if sign==1 else x.high>p.price) for x in bars[p.index+1:si])]
            if not fresh:
                ref = crossed[-1]
                reject('liquidity_already_swept'); continue
            ref = fresh[-1]
            # Reclaim may close on either of the next two bars. Do not require
            # a five-minute reversal to fit exactly inside one candle boundary.
            ri = next((i for i in range(si, min(si+3,len(bars)))
                       if sign*(bars[i].close-ref.price)>0), None)
            if ri is None:
                reject('sweep_not_reclaimed'); continue
            extreme = min(x.low for x in bars[si:ri+1]) if sign==1 else max(x.high for x in bars[si:ri+1])
            stage('sweep_reclaimed')
            if any(sign*(b.close-pivot.price)>0 for b in bars[pivot.index+1:si]):
                reject('structure_already_broken'); continue
            stage('fresh_sweep')
            higher_break = countertrend_break(series['15m'], sweep.time_ms, sign) if countertrend else None
            if countertrend and higher_break is None:
                reject('opposing_hourly_structure'); continue
            for bi in range(ri, min(si+7,len(bars))):
                b = bars[bi]
                stage('break_bars_examined')
                if any(sign*(x.close-extreme)<=0 for x in bars[si+1:bi+1]):
                    reject('sweep_invalidated_before_break'); break
                if sign*(b.close-pivot.price)<=0:
                    reject('no_structure_break'); continue
                if bi == si and not same_bar_sequence(series.get('1m', []), sweep, ref.price, pivot.price, sign):
                    reject('ambiguous_sweep_break_order'); break
                stage('structure_break')
                local_atr = atr(bars[:bi])
                body = displacement_body(b, sign)
                if local_atr<=0 or body < local_atr or body < .6*(b.high-b.low):
                    reject('weak_break'); break
                stage('displacement_confirmed')
                # Evaluate both entry-zone types independently after the same structural proof.
                left=bars[bi-1]
                right=bars[bi+1] if bi+1<len(bars) else None
                choices=[]
                if right is not None:
                    flo,fhi=(left.high,right.low) if sign==1 else (right.high,left.low)
                    if flo<fhi:
                        choices.append(('FVG',flo,fhi,right.time_ms))
                opposite=[x for x in bars[si:bi] if sign*(x.close-x.open)<0]
                if opposite:
                    origin=opposite[-1]
                    if sign*(b.close-(origin.high if sign==1 else origin.low))>0:
                        choices.append(('OB',origin.low,origin.high,origin.time_ms))
                if not choices:
                    reject('no_fvg_or_order_block'); break
                for poi_kind,lo,hi,poi_origin in choices:
                    stage('fvg_formed' if poi_kind=='FVG' else 'order_block_formed')
                    born=(b.time_ms if poi_kind=="OB" else right.time_ms)+STEP
                    post_zone=bars[bi+1:] if poi_kind=="OB" else bars[bi+2:]
                    if as_of_ms-born>MAX_AGE:
                        reject('stale_zone'); continue
                    later_minutes = [x for x in series.get('1m', [])
                                     if born <= x.time_ms and x.time_ms+60_000 <= as_of_ms]
                    # Entry uses the actual zone midpoint, never a model-provided number.
                    entry=round((lo+hi)/2,1)
                    if any((x.low<=entry if sign==1 else x.high>=entry) for x in [*post_zone, *later_minutes]):
                        reject('already_retested'); continue
                    invalidation=(min(extreme,lo) if sign==1 else max(extreme,hi)) if poi_kind=="OB" else extreme
                    stop=_tick(invalidation-sign*max(.25*vol,.1),sign)
                    # Widen a structurally valid tight stop to one ATR. Never
                    # move it inside invalidation or shrink a wide structural stop.
                    if sign*(entry-stop)<vol:
                        stop=_tick(entry-sign*vol,sign)
                    risk=sign*(entry-stop)
                    if not vol<=risk<=3*vol:
                        reject('stop_outside_atr_band'); continue
                    if sign*(live-entry)<=0 or abs(live-entry)>3*vol:
                        reject('quote_not_waiting_for_pullback'); continue
                    levels=[z['price']-sign*.1*vol for z in zones['support_resistance']
                            if z['timeframe'] in ('1h','4h') and z['kind']==('resistance' if sign==1 else 'support')
                            and sign*(z['price']-entry)>0]
                    for tf in ('15m','1h'):
                        ss=series[tf]
                        for p in find_swings(ss):
                            if p.kind!=('HIGH' if sign==1 else 'LOW') or sign*(p.price-entry)<=0:
                                continue
                            if not any(sign*(x.close-p.price)>0 for x in ss[p.index+1:]):
                                levels.append(p.price-sign*.1*vol)
                    if not levels:
                        reject('no_structural_target'); continue
                    obstacle=min(levels) if sign==1 else max(levels)
                    target=target_at_net_rr(costs,direction,entry,stop,obstacle)
                    if target is None:
                        reject('insufficient_net_reward_before_obstacle'); continue
                    if sign*(live-target)>=0 or any((x.high>=target if sign==1 else x.low<=target) for x in post_zone):
                        reject('target_consumed'); continue
                    evidence={'sweep_ms':sweep.time_ms,'sweep_extreme':extreme,
                              'sweep_ohlc':[sweep.open,sweep.high,sweep.low,sweep.close],
                              'break_ohlc':[b.open,b.high,b.low,b.close],
                              'reclaim_ms':bars[ri].time_ms,
                              'reclaim_ohlc':[bars[ri].open,bars[ri].high,bars[ri].low,bars[ri].close],
                              'countertrend':countertrend,'countertrend_15m_break':higher_break,
                              'same_bar_sequence_1m':[[x.time_ms,x.open,x.high,x.low,x.close]
                                  for x in series.get('1m',[]) if sweep.time_ms<=x.time_ms<sweep.time_ms+STEP] if bi==si else [],
                              'swept_pivot_ms':ref.time_ms,'swept_price':ref.price,
                              'break_ms':b.time_ms,'broken_pivot_ms':pivot.time_ms,'broken_price':pivot.price,
                              'fvg_ms':right.time_ms if poi_kind=='FVG' else None,
                              'fvg_origin_ms':[left.time_ms,b.time_ms,right.time_ms] if poi_kind=='FVG' else [],
                              'poi_kind':poi_kind,'poi_origin_ms':poi_origin,
                              'zone_context':zones,'atr14':vol,'stop_atr':risk/vol,'obstacle':obstacle,'hourly_regime':regime}
                    raw=dict(version=VERSION,direction=direction,entry=entry,stop=stop,target=target,
                             zone=[lo,hi],invalidation=invalidation,formed_ms=born,expires_ms=born+TTL,evidence=evidence)
                    raw['opportunity_id']=f'{VERSION}:{direction}:{sweep.time_ms}:{b.time_ms}:{poi_kind}:{poi_origin}'
                    raw['candidate_id']=hashlib.sha256(json.dumps(raw,sort_keys=True).encode()).hexdigest()[:24]
                    raw['as_of_ms']=as_of_ms
                    stage('candidate_built')
                    candidates.append(raw)
                break
            else:
                reject('break_window_exhausted')
    unique={c['candidate_id']:c for c in candidates}
    result = sorted(unique.values(),key=lambda c:c['formed_ms'],reverse=True)[:2]
    if audit is not None:
        audit['status'] = 'scanned'
        audit['candidate_count'] = len(result)
        audit['rejection_counts'] = dict(diagnostics)
    return result,diagnostics


def flat(reason, diagnostics=None):
    return {'strategy_id':'ict_reversal','decision':'NO_TRADE','final_ai_decision':'NO_TRADE',
            'setup_mode':'flat','trade_reason':reason,'no_trade_reasons':[reason],
            'strategy_candidates':{},'verified_plan_review':{'version':VERSION,'diagnostics':diagnostics or {}}}


def apply_review(review, candidates, now_ms):
    """Allowlist output. Never merge model fields into an executable contract."""
    if not isinstance(review,dict) or review.get('approve') is not True:
        return flat(str(review.get('reason_fa','رد تحلیل‌گر'))[:1200] if isinstance(review,dict) else 'پاسخ نامعتبر تحلیل‌گر')
    c=next((c for c in candidates if c['candidate_id']==review.get('candidate_id')),None)
    # Review is not an order. Keep the original observation time, and let the
    # order gate replay every closed minute since then before submitting.
    if c is None or now_ms>=c['expires_ms'] or now_ms<c['as_of_ms']:
        return flat('شناسهٔ نامعتبر یا فرصت منقضی پس از پاسخ تحلیل‌گر')
    c=deepcopy(c);reason=str(review.get('reason_fa') or 'ستاپ کندلی تأیید شد')[:1200]
    c['approved_at_ms']=now_ms
    return {'strategy_id':'ict_reversal','decision':'NO_TRADE','final_ai_decision':'WAIT',
            'setup_mode':'conditional','potential_direction':c['direction'],
            'entry_price':c['entry'],'entry_zone_low':c['zone'][0],'entry_zone_high':c['zone'][1],
            'conditional_entry_zone_low':c['zone'][0],'conditional_entry_zone_high':c['zone'][1],
            'stop_loss':c['stop'],'take_profit':c['target'],'structural_invalidation_level':c['invalidation'],
            'confirmation_type':'sweep_mss_poi','confirmation_timeframe':'5m',
            'confirmation_required':'جاروب و شکست ساختار از کندل بسته تأیید شده؛ سفارش محدود فقط برای بازگشت تازه معتبر است.',
            'conditional_invalidation':'عبور قیمت از نقطهٔ ابطال ساختاری یا پایان مهلت ورود',
            'market_regime':c['evidence']['hourly_regime'],'market_regime_confidence':'low',
            'market_context_caution_ack':True,'trade_against_dominant_trend':bool(c['evidence'].get('countertrend')),
            'trade_reason':reason,'uncertainty_notes':str(review.get('risk_fa') or '')[:1200],
            'setup_timeframe':'intraday_5m_15m','entry_poi_type':c['evidence'].get('poi_kind','FVG'),
            'liquidity_event_clear':True,'structural_confirmation_clear':True,
            'logical_invalidation_clear':True,'poi_reaction_clear':False,
            'proposed_setup':{'new_setup':True,'distinct_new_setup':False,'thesis_fa':reason,
                              'expires_hours':max(0,(c['expires_ms']-now_ms)/3600000)},
            'verified_trade_plan':c,'strategy_candidates':{}}


def plan_error(data, now_ms=None):
    """Binding check for persisted server contract, used again at order creation."""
    c=data.get('verified_trade_plan') or {}
    try:
        binding = {k:v for k,v in c.items() if k not in ('candidate_id','as_of_ms','approved_at_ms')}
        if hashlib.sha256(json.dumps(binding,sort_keys=True).encode()).hexdigest()[:24] != c['candidate_id']:
            return 'verified_plan_contract_changed'
        from app.services.multi_setup_analyst import VERSION as ANALYST_VERSION
        if c['version'] not in (VERSION,ANALYST_VERSION) or c['direction'] not in ('LONG','SHORT'):
            return 'verified_plan_missing'
        if c['version']==ANALYST_VERSION and any(data.get(k)!=c.get(k) for k in ('strategy_id','confirmation_type','breakout_level')):
            return 'verified_plan_contract_changed'
        pairs={'entry_price':'entry','stop_loss':'stop','take_profit':'target','structural_invalidation_level':'invalidation'}
        if any(not math.isfinite(float(data[k])) or abs(float(data[k])-float(c[v]))>1e-8 for k,v in pairs.items()):
            return 'verified_plan_levels_changed'
        if data['potential_direction']!=c['direction'] or [data['entry_zone_low'],data['entry_zone_high']]!=c['zone']:
            return 'verified_plan_levels_changed'
        if now_ms is not None and now_ms>=c['expires_ms']:
            return 'verified_plan_expired'
        return None
    except (KeyError,ValueError,TypeError,OverflowError):
        return 'verified_plan_invalid'


async def unreviewed_candidates(session, candidates, now):
    from datetime import timedelta
    from sqlalchemy import select
    from app.models import AiAnalysisRun
    start = now.astimezone(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0)
    runs = (await session.execute(select(AiAnalysisRun).where(AiAnalysisRun.created_at >= now-timedelta(days=1)))).scalars().all()
    reviewed=set();calls=0
    for run in runs:
        meta=(run.structured or {}).get('verified_plan_review') or {}
        reviewed.update(meta.get('opportunity_ids') or [])
        created=run.created_at.replace(tzinfo=timezone.utc) if run.created_at.tzinfo is None else run.created_at
        if meta.get('api_attempted') and created>=start:
            calls+=1
    if calls >= 4:
        return [], 'daily_api_review_limit'
    fresh=[c for c in candidates if c['opportunity_id'] not in reviewed]
    return fresh, None if fresh else 'opportunity_already_reviewed'
