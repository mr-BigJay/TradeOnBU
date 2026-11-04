"""A read-only market observer with its own journal. No order or wallet access."""
import math
import os
import time
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from app.models import StructureObservation, SimPositionSettings
from app.services.ict_execution_plan import STEP, structural_target, target_at_two_r
from app.services.ict_structure import find_swings, DISPLACEMENT_BODY_RATIO
from app.services.ict_confirmation_engine import _token_on_bar

MIN = 60_000
HOUR = 60 * MIN
ACTIVE = ("await_touch", "limit", "filled")


def enabled():
    return os.getenv("ICT_STRUCTURE_SHADOW") == "1"


def valid_series(bars, step):
    return all(math.isfinite(x) and x > 0 for b in bars for x in (b.open, b.high, b.low, b.close)) and all(
        b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high for b in bars
    ) and all(b.time_ms % step == 0 for b in bars) and all(
        b.time_ms - a.time_ms == step for a, b in zip(bars, bars[1:]))


def detect(bars, direction, variant):
    """Scan closed bars. A pivot is known only after its two right-hand candles."""
    if len(bars) < 24 or not valid_series(bars, STEP):
        return []
    long = direction == "LONG"
    sign = 1 if long else -1
    swings = find_swings(bars)
    found = []
    for i in range(max(14, len(bars)-24), len(bars)-2):
        b = bars[i]
        known = [p for p in swings if p.index + 2 < i]
        highs = [p for p in known if p.kind == "HIGH"]
        lows = [p for p in known if p.kind == "LOW"]
        if variant == "swing":
            if min(len(highs), len(lows)) < 2:
                continue
            # Reversal requires an opposite pre-sweep trend (lower highs and
            # lower lows for LONG). A close in the old trend is continuation.
            if any(sign*(p[-1].price-p[-2].price) >= 0 for p in (highs,lows)):
                continue
            swept = (lows if long else highs)[-1]
            broken = (highs if long else lows)[-1]
            level, reference = swept.price, swept.time_ms
            # Do not reuse an already swept or broken pivot.
            if any((c.low < level if long else c.high > level) for c in bars[swept.index+1:i]):
                continue
            if any(sign*(c.close-broken.price)>0 for c in bars[broken.index+1:i+1]):
                continue
        else:
            ref = (min(bars[i-12:i], key=lambda c:c.low) if long else max(bars[i-12:i], key=lambda c:c.high))
            level, reference = (ref.low if long else ref.high), ref.time_ms
        extreme = b.low if long else b.high
        if sign*(level-extreme)<=0 or sign*(b.close-level)<=0:
            continue
        for j in range(i+1, min(i+13,len(bars))):
            c = bars[j]
            if sign*(c.close-extreme)<=0:
                break
            if variant == "swing":
                price, pivot_time = broken.price, broken.time_ms
                if sign*(c.close-price)<=0:
                    continue
                if sign*(c.close-c.open) < DISPLACEMENT_BODY_RATIO*(c.high-c.low):
                    break
                token = "MSS"
            else:
                window=bars[:j+1]
                token=next((k for k in ("mss","bos") if _token_on_bar(k,direction=direction,window=window,
                            zone_lo=0,zone_hi=0,structured={})),None)
                if token is None:
                    continue
                n=12 if token=="mss" else 8
                ref=max(bars[j-n:j],key=lambda k:k.high) if long else min(bars[j-n:j],key=lambda k:k.low)
                price,pivot_time=(ref.high if long else ref.low),ref.time_ms
                token=token.upper()
            # A directional three-bar FVG formed by the break or its next bar.
            for k in range(max(j,i+2),min(j+2,len(bars))):
                left,right=bars[k-2],bars[k]
                lo,hi=(left.high,right.low) if long else (right.high,left.low)
                if lo>=hi:
                    continue
                key=f"{variant}:{direction}:{b.time_ms}:{c.time_ms}:{right.time_ms}"
                reference_index = next(n for n,x in enumerate(bars) if x.time_ms==reference)
                pivot_index = next(n for n,x in enumerate(bars) if x.time_ms==pivot_time)
                visible=bars[max(0,min(reference_index,pivot_index,i,k-2)-2):k+1]
                found.append(dict(key=key,variant=variant,direction=direction,sweep_ms=b.time_ms,
                    reference_ms=reference,reference=level,extreme=extreme,break_ms=c.time_ms,
                    pivot_ms=pivot_time,pivot=price,token=token,fvg_ms=right.time_ms,
                    zone=[lo,hi],origin=[bars[m].time_ms for m in (k-2,k-1,k)],
                    bars=[[x.time_ms,x.open,x.high,x.low,x.close] for x in visible]))
                break
            break
    return found


def start(e, bars, sim, now, minutes=()):
    lo,hi=e["zone"]
    later=[c for c in bars if c.time_ms>e["fvg_ms"]]
    long=e["direction"]=="LONG"
    sign=1 if long else -1
    entry=round((lo+hi)/2,1)
    stop=round(e["extreme"]-sign*.1,1)
    track=dict(entry=entry,stop=stop,expires_ms=e["fvg_ms"]+STEP+HOUR,
               cursor=(now//MIN)*MIN-MIN, registered_ms=now, costs={k:getattr(sim,k) for k in
               ("default_margin_usdt","default_leverage","maker_fee_rate","taker_fee_rate","slippage_bps","default_tp_behavior")})
    for c in later:
        if sign*(c.close-stop)<=0:
            return "invalidated",track
        if c.low<=hi and c.high>=lo:
            track["touch_ms"]=c.time_ms
            return "late_touch",track
    # A completed minute after the latest closed 5m bar can already contain
    # the first retest. The in-progress registration minute is indeterminate.
    for b in minutes:
        if bars[-1].time_ms+STEP <= b.time_ms and b.time_ms+MIN<=now and b.low<=hi and b.high>=lo:
            track["touch_ms"]=b.time_ms
            return "late_touch",track
    if sign*(entry-stop)<=0:
        return "invalidated",track
    if now>=track["expires_ms"]:
        return "expired",track
    return "await_touch",track


def advance(row, five, minutes, now):
    """Advance chronologically; each closed minute is consumed at most once."""
    e=row.evidence
    t=dict(row.tracking)
    state=row.state
    sign=1 if e["direction"]=="LONG" else -1
    entry,stop=t["entry"],t["stop"]
    lo,hi=e["zone"]
    costs=SimpleNamespace(**t["costs"])
    for b in minutes:
        if b.time_ms<=t["cursor"] or b.time_ms+MIN>now:
            continue
        if b.time_ms!=t["cursor"]+MIN or not valid_series([b],MIN):
            state="data_gap"
            break
        t["cursor"]=b.time_ms
        if b.time_ms < t.get("registered_ms",0):
            if b.low<=hi and b.high>=lo:
                t["touch_ms"]=b.time_ms
                state="registration_ambiguous"
                break
            continue
        if state!="filled" and b.time_ms>=t["expires_ms"]:
            state="expired"
            break
        if state=="await_touch":
            if b.low<=hi and b.high>=lo:
                t.setdefault("touch_ms",b.time_ms)
            if (b.time_ms+MIN)%STEP:
                continue
            history=[c for c in five if c.time_ms+STEP<=b.time_ms+MIN][-120:]
            if len(history)<18 or history[-1].time_ms!=b.time_ms+MIN-STEP or not valid_series(history,STEP):
                state="data_gap"
                break
            if sign*(history[-1].close-stop)<=0:
                state="invalidated"
                break
            if "touch_ms" not in t:
                continue
            cap=entry+sign*2.5*abs(entry-stop)
            if target_at_two_r(costs,e["direction"],entry,stop,cap) is None:
                state="cost_cap"
                break
            tp,_=structural_target(costs,e["direction"],entry,stop,cap,history)
            if tp is None:
                state="obstacle"
                break
            state="limit"
            t.update(tp=tp,ready_ms=b.time_ms+MIN)
            continue
        tp=t["tp"]
        hit_tp=b.high>=tp if sign==1 else b.low<=tp
        hit_sl=b.low<=stop if sign==1 else b.high>=stop
        if state=="limit":
            touched=b.low<=entry if sign==1 else b.high>=entry
            if not touched:
                if hit_tp:
                    state="target_before_fill"
                    break
                continue
            t["fill_ms"]=b.time_ms
            state="filled"
            if hit_tp and not hit_sl and sign*(b.open-entry)>0 and sign*(b.close-tp)<0:
                state="ambiguous"
                break
        if hit_tp and hit_sl:
            state="ambiguous"
            break
        if hit_tp or hit_sl:
            state="tp" if hit_tp else "sl"
            px=tp if hit_tp else (min(stop,b.open) if sign==1 else max(stop,b.open))*(1-sign*costs.slippage_bps/10000)
            rate=costs.maker_fee_rate if hit_tp and costs.default_tp_behavior=="limit" else costs.taker_fee_rate
            qty=costs.default_margin_usdt*costs.default_leverage/entry
            t.update(exit_ms=b.time_ms,net_usdt=qty*(sign*(px-entry)-entry*costs.maker_fee_rate-px*rate))
            break
    row.state,row.tracking=state,t


async def tick():
    from app.db import SessionLocal
    from app.services.market_data import MarketDataService
    from app.services.entry_strategies import A, strategy_settings
    from app.services.entry_execution_settings import get_paper_entry_execution_config
    async with SessionLocal() as s:
        sim=await s.get(SimPositionSettings,1)
        if not sim or not (await strategy_settings(s))[A] or (await get_paper_entry_execution_config(s))[0]!="STRICT":
            return
        market=MarketDataService()
        five=(await market.fetch_kline_bundle("BTCUSDT","5m",160)).closed
        minutes=(await market.fetch_kline_bundle("BTCUSDT","1m",200)).closed
        now=int(time.time()*1000)
        active=(await s.scalars(select(StructureObservation).where(StructureObservation.state.in_(ACTIVE)))).all()
        for row in active:
            advance(row,five,minutes,now)
        if (five and minutes and minutes[-1].time_ms == now//MIN*MIN-MIN
                and valid_series(minutes[-6:], MIN) and five[-1].time_ms==now//STEP*STEP-STEP):
            for variant in ("rolling","swing"):
                for direction in ("LONG","SHORT"):
                    for e in detect(five,direction,variant):
                        state,tracking=start(e,five,sim,now,minutes)
                        await s.execute(insert(StructureObservation).values(key=e["key"],observed_ms=now,
                            state=state,evidence=e,tracking=tracking).on_conflict_do_nothing(index_elements=["key"]))
        await s.commit()


REVIEW = """
Independently review each supplied shadow_candidate against the closed 1h and 4h context.
Include shadow_review as an array of {key, verdict: "watch" or "skip", reason_fa}.
Copy its exact key; its referenced swings, break and zone are immutable. Do not use
this secondary review to change your primary plan. This review cannot place an order.
"""


async def reviewable(session):
    rows = (await session.scalars(select(StructureObservation).where(
        StructureObservation.state=="await_touch",StructureObservation.key.like("swing:%"),
        StructureObservation.observed_ms>int(time.time()*1000)-HOUR,
    ).order_by(StructureObservation.observed_ms.desc()).limit(8))).all()
    return [{k:v for k,v in row.evidence.items() if k!="bars"} for row in rows]


def validate_review(candidates, raw):
    raw=raw if isinstance(raw,list) else []
    result={}
    for c in candidates:
        matches=[r for r in raw if isinstance(r,dict) and r.get("key")==c["key"]]
        verdict,reason="unreviewed",""
        if len(matches)==1 and matches[0].get("verdict") in ("watch","skip"):
            verdict=matches[0]["verdict"]
            reason=str(matches[0].get("reason_fa") or "")[:240]
        result[c["key"]]={"verdict":verdict,"reason":reason}
    return result
