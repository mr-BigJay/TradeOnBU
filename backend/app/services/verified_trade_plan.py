"""Versioned, candle-derived BTC plans. The model can veto, never set prices.

All inputs are closed bars available at as_of_ms. No optimised parameters or
claims of profitability: v1 is a fixed, falsifiable paper-trading baseline.
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
from app.services.ict_execution_plan import target_at_two_r

REASONS_FA = {
 'verified_plan_missing':'سناریوی قدیمی؛ قرارداد کندلی جدید ندارد.',
 'verified_plan_invalid':'قرارداد کندلی ناقص است.',
 'verified_plan_expired':'مهلت فرصت کندلی پایان یافته است.',
 'verified_plan_levels_changed':'سطوح سناریو با قرارداد تأییدشده یکسان نیست.',
 'verified_plan_contract_changed':'شواهد یا سطوح قرارداد تغییر کرده است.',
 'verified_market_data_invalid':'کندل‌های تازه سالم و پیوسته نیستند.',
 'verified_freshness_unavailable':'پوشش یک‌دقیقه‌ای برای بررسی تازگی کافی نیست.',
 'verified_pullback_already_used':'قیمت پیش از ثبت سفارش به ناحیه برگشته؛ فرصت مصرف شده است.',
 'verified_one_position_limit':'یک پوزیشن یا سفارش فعال وجود دارد.',
 'verified_daily_loss_limit':'سقف زیان روزانه رسیده؛ ورود تا روز بعد متوقف است.',
 'verified_no_risk_budget':'بودجهٔ ریسک یا موجودی آزاد کافی نیست.',
}
VERSION = 'verified_reversal_v1'
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
new evidence. Assess structural coherence and remaining room to target.
'''


def enabled():
    return os.getenv('VERIFIED_PLANS_ENABLED', '0') == '1'


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


def build_candidates(series, costs, as_of_ms, live):
    """Sweep of a known pivot, opposite-swing close break with displacement,
    fresh three-candle FVG, ATR-buffered invalidation, net 2R before obstacles.
    Return candidates + explicit rejection counters (no LLM calls).
    """
    diagnostics = {}
    def reject(reason):
        diagnostics[reason] = diagnostics.get(reason, 0) + 1
    for tf, step in (('5m', STEP), ('15m', 3*STEP), ('1h', 12*STEP)):
        if not valid_bars(series.get(tf, []), step, as_of_ms, 30):
            return [], {'invalid_or_stale_'+tf: 1}
    if '1m' in series and not valid_bars(series['1m'], 60_000, as_of_ms, 20):
        return [], {'invalid_or_stale_1m': 1}
    if not isinstance(live, (int, float)) or not math.isfinite(live) or live <= 0:
        return [], {'invalid_quote': 1}
    bars = series['5m']; pivots = find_swings(bars); vol = atr(bars)
    regime = trend(series['1h'])
    candidates = []
    for direction, sign in (('LONG', 1), ('SHORT', -1)):
        if regime == ('bearish' if sign == 1 else 'bullish'):
            reject('opposing_hourly_structure'); continue
        for si in range(max(16, len(bars)-18), len(bars)-2):
            sweep = bars[si]
            known = [p for p in pivots if p.index+2 < si]
            swept = [p for p in known if p.kind == ('LOW' if sign==1 else 'HIGH')]
            broken = [p for p in known if p.kind == ('HIGH' if sign==1 else 'LOW')]
            if not swept or not broken:
                continue
            ref, pivot = swept[-1], broken[-1]
            extreme = sweep.low if sign==1 else sweep.high
            if sign*(ref.price-extreme) <= 0 or sign*(sweep.close-ref.price) <= 0:
                continue
            if any((b.low < ref.price if sign==1 else b.high > ref.price) for b in bars[ref.index+1:si]):
                continue
            if any(sign*(b.close-pivot.price)>0 for b in bars[pivot.index+1:si+1]):
                continue
            for bi in range(si+1, min(si+7,len(bars)-1)):
                b = bars[bi]
                if any(sign*(x.close-extreme)<=0 for x in bars[si+1:bi+1]):
                    break
                if sign*(b.close-pivot.price)<=0:
                    continue
                local_atr = atr(bars[:bi])
                body = displacement_body(b, sign)
                if local_atr<=0 or body < local_atr or body < .6*(b.high-b.low):
                    reject('weak_break'); break
                # The displacement candle is the middle of this FVG.
                left,right=bars[bi-1],bars[bi+1]
                lo,hi=(left.high,right.low) if sign==1 else (right.high,left.low)
                if lo>=hi:
                    reject('no_displacement_fvg'); break
                born=right.time_ms+STEP
                if as_of_ms-born>MAX_AGE:
                    reject('stale_zone'); break
                later_minutes = [x for x in series.get('1m', [])
                                 if born <= x.time_ms and x.time_ms+60_000 <= as_of_ms]
                if any(x.low<=hi and x.high>=lo for x in [*bars[bi+2:], *later_minutes]):
                    reject('already_retested'); break
                # Entry uses the actual FVG midpoint, never a model-provided number.
                entry=round((lo+hi)/2,1)
                stop=_tick(extreme-sign*max(.25*vol,.1),sign)
                risk=sign*(entry-stop)
                if not vol<=risk<=3*vol:
                    reject('stop_outside_atr_band'); break
                if sign*(live-hi if sign==1 else live-lo)<=0 or abs(live-entry)>3*vol:
                    reject('quote_not_waiting_for_pullback'); break
                levels=[]
                for tf in ('15m','1h'):
                    ss=series[tf]
                    for p in find_swings(ss):
                        if p.kind!=('HIGH' if sign==1 else 'LOW') or sign*(p.price-entry)<=0:
                            continue
                        if not any(sign*(x.close-p.price)>0 for x in ss[p.index+1:]):
                            levels.append(p.price-sign*.1*vol)
                if not levels:
                    reject('no_structural_target'); break
                obstacle=min(levels) if sign==1 else max(levels)
                target=target_at_two_r(costs,direction,entry,stop,obstacle)
                if target is None:
                    reject('insufficient_net_reward_before_obstacle'); break
                if sign*(live-target)>=0 or any((x.high>=target if sign==1 else x.low<=target) for x in bars[bi+2:]):
                    reject('target_consumed'); break
                evidence={'sweep_ms':sweep.time_ms,'sweep_extreme':extreme,
                          'sweep_ohlc':[sweep.open,sweep.high,sweep.low,sweep.close],
                          'break_ohlc':[b.open,b.high,b.low,b.close],
                          'swept_pivot_ms':ref.time_ms,'swept_price':ref.price,
                          'break_ms':b.time_ms,'broken_pivot_ms':pivot.time_ms,'broken_price':pivot.price,
                          'fvg_ms':right.time_ms,'fvg_origin_ms':[left.time_ms,b.time_ms,right.time_ms],
                          'atr14':vol,'stop_atr':risk/vol,'obstacle':obstacle,'hourly_regime':regime}
                raw=dict(version=VERSION,direction=direction,entry=entry,stop=stop,target=target,
                         zone=[lo,hi],invalidation=extreme,formed_ms=born,expires_ms=born+TTL,evidence=evidence)
                raw['opportunity_id']=f'{VERSION}:{direction}:{sweep.time_ms}:{b.time_ms}:{right.time_ms}'
                raw['candidate_id']=hashlib.sha256(json.dumps(raw,sort_keys=True).encode()).hexdigest()[:24]
                raw['as_of_ms']=as_of_ms
                candidates.append(raw);break
    unique={c['candidate_id']:c for c in candidates}
    return sorted(unique.values(),key=lambda c:c['formed_ms'],reverse=True)[:2],diagnostics


def flat(reason, diagnostics=None):
    return {'strategy_id':'ict_reversal','decision':'NO_TRADE','final_ai_decision':'NO_TRADE',
            'setup_mode':'flat','trade_reason':reason,'no_trade_reasons':[reason],
            'strategy_candidates':{},'verified_plan_review':{'version':VERSION,'diagnostics':diagnostics or {}}}


def apply_review(review, candidates, now_ms):
    """Allowlist output. Never merge model fields into an executable contract."""
    if not isinstance(review,dict) or review.get('approve') is not True:
        return flat(str(review.get('reason_fa','رد تحلیل‌گر'))[:1200] if isinstance(review,dict) else 'پاسخ نامعتبر تحلیل‌گر')
    c=next((c for c in candidates if c['candidate_id']==review.get('candidate_id')),None)
    if c is None or now_ms>=c['expires_ms'] or now_ms-c['as_of_ms']>120_000:
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
            'market_context_caution_ack':True,'trade_against_dominant_trend':False,
            'trade_reason':reason,'uncertainty_notes':str(review.get('risk_fa') or '')[:1200],
            'setup_timeframe':'intraday_5m_15m','entry_poi_type':'FVG',
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
        if c['version']!=VERSION or c['direction'] not in ('LONG','SHORT'):
            return 'verified_plan_missing'
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
