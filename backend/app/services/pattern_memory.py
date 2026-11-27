"""Immutable AI-selected HTF drawings, replayed on closed source candles."""
from app.services.chart_patterns import NAMES
from app.services.verified_trade_plan import atr

STEPS = {"1h":3600000, "4h":14400000}


def _price(edge, stamp):
    (t, price), _ = edge["anchors"]
    return price + (stamp-t)*edge["slope"]


def _edge(stamps, candles, side):
    if not isinstance(stamps,list) or len(stamps)!=2 or not all(type(t) is int and t in candles for t in stamps):
        return None
    a,b = stamps
    if b<=a:
        return None
    p,q = (getattr(candles[t],side) for t in stamps)
    return {"anchors":[[a,p],[b,q]],"slope":(q-p)/(b-a)}


def _broad_enough(kind, tf, upper, lower):
    if kind != "descending_channel":
        return True
    # Six 4h intervals between the two highs and between the two lows.
    return tf == "4h" and all(
        edge["anchors"][1][0] - edge["anchors"][0][0] >= 6 * STEPS["4h"]
        for edge in (upper, lower)
    )


def _position(pattern, bar, step, tolerance):
    stamp = bar.time_ms+step
    upper, lower = (_price(pattern[k],stamp) for k in ("upper","lower"))
    if upper<=lower:
        return "converged"
    if bar.close>upper+tolerance:
        return "above"
    if bar.close<lower-tolerance:
        return "below"
    if bar.close>=upper-tolerance:
        return "near_upper"
    return "near_lower" if bar.close<=lower+tolerance else "inside"


def update_drawings(previous, proposals, series, now_ms):
    old = ((previous or {}).get("analyst_summary") or {}).get("drawing_memory") or {}
    active, ended = [], []
    cutoffs = dict(old.get("replacement_after") or {})
    closed = {tf:[b for b in series.get(tf,[]) if b.time_ms+step<=now_ms] for tf,step in STEPS.items()}
    tolerance = {tf:.2*atr(bars) for tf,bars in closed.items()}
    for item in old.get("active",[]):
        tf = item["timeframe"]
        if not _broad_enough(item["kind"], tf, item["upper"], item["lower"]):
            continue
        pattern = {**item, "upper":dict(item["upper"]), "lower":dict(item["lower"])}
        for bar in closed[tf]:
            stamp = bar.time_ms+STEPS[tf]
            if stamp<=pattern["as_of_ms"]:
                continue
            pattern.pop("break_status", None)  # Superseded by a closed source candle.
            pos = _position(pattern,bar,STEPS[tf],tolerance[tf])
            pattern.update(position=pos,as_of_ms=stamp)
            for side in ("upper","lower"):
                pattern[side]["price"] = _price(pattern[side],stamp)
            if pos in ("above","below","converged"):
                ended.append(pattern)
                cutoffs[tf+":"+pattern["kind"]] = stamp
                break
        else:
            active.append(pattern)
    candidates = proposals[:6] if isinstance(proposals,list) else []
    channel_proposals = sum(isinstance(row,dict) and row.get("kind") in ("ascending_channel","descending_channel") for row in candidates)
    for row in candidates:
        if not isinstance(row,dict):
            continue
        tf,kind = row.get("timeframe"),row.get("kind")
        if not isinstance(tf,str) or tf not in STEPS or not isinstance(kind,str) or kind not in NAMES:
            continue
        if any(p["kind"]==kind and p["timeframe"]==tf for p in active):
            continue
        bars = closed[tf]
        candles = {b.time_ms:b for b in bars}
        upper = _edge(row.get("upper"),candles,"high")
        lower = _edge(row.get("lower"),candles,"low")
        if not upper or not lower or not _broad_enough(kind, tf, upper, lower):
            continue
        first = max(e["anchors"][0][0] for e in (upper,lower))
        last = min(e["anchors"][1][0] for e in (upper,lower))
        newest = max(e["anchors"][1][0] for e in (upper,lower))
        if first>=last or newest < cutoffs.get(tf+":"+kind,0):
            continue
        u,l = upper["slope"],lower["slope"]
        parallel = abs(u-l)*(last-first)<=max(tolerance[tf],.2*max(abs(u),abs(l))*(last-first))
        if not {"ascending_channel":parallel and u>0 and l>0,"descending_channel":parallel and u<0 and l<0,
                "rising_wedge":0<u<l,"falling_wedge":u<l<0,"triangle":u<=0<=l and u<l,
                "range":max(abs(u),abs(l))*(last-first)<=tolerance[tf]}[kind]:
            continue
        pattern = dict(kind=kind,timeframe=tf,upper=upper,lower=lower,formed_ms=now_ms,
                       as_of_ms=bars[-1].time_ms+STEPS[tf])
        if any(_position(pattern,b,STEPS[tf],tolerance[tf]) in ("above","below","converged")
               for b in bars if b.time_ms>=first):
            continue
        for edge in (upper,lower):
            edge["price"] = _price(edge,pattern["as_of_ms"])
        pattern["position"] = _position(pattern,bars[-1],STEPS[tf],tolerance[tf])
        active.append(pattern)
    return dict(active=active,ended=ended,replacement_after=cutoffs,channel_proposals=channel_proposals)
