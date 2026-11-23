"""Descriptive pattern context built from confirmed pivots at two swing scales."""
from app.services.ict_structure import find_swings
from app.services.verified_trade_plan import atr

NAMES = {"rising_wedge": "گوهٔ صعودی", "falling_wedge": "گوهٔ نزولی",
         "ascending_channel": "کانال صعودی", "descending_channel": "کانال نزولی",
         "triangle": "مثلث همگرا", "range": "محدودهٔ افقی"}
POSITIONS = {"above": "بالای سقف", "below": "زیر کف", "near_upper": "نزدیک سقف",
             "near_lower": "نزدیک کف", "inside": "داخل الگو"}


def _boundary(points, bars, step, width, sign):
    if len(points) < 2:
        return None
    a, b = points[-2:]
    if b.index - a.index < 3 or len(bars)-1-b.index > 2*(b.index-a.index):
        return None
    slope = (b.price-a.price)/(b.time_ms-a.time_ms)
    def price(t):
        return b.price+slope*(t-b.time_ms)
    if any(sign*(bar.close-price(bar.time_ms+step)) > width
           for bar in bars[a.index:-1]):
        return None
    return dict(anchors=[[a.time_ms,a.price],[b.time_ms,b.price]], slope=slope,
                price=price(bars[-1].time_ms+step))


def detect(bars, step, radius):
    if len(bars) < 2*radius+3:
        return None
    swings = find_swings(bars, left=radius, right=radius)
    tolerance = atr(bars)*.2
    upper = _boundary([p for p in swings if p.kind=="HIGH"],bars,step,tolerance,1)
    lower = _boundary([p for p in swings if p.kind=="LOW"],bars,step,tolerance,-1)
    if not upper or not lower:
        return None
    start = max(upper["anchors"][0][0],lower["anchors"][0][0])
    end = min(upper["anchors"][1][0],lower["anchors"][1][0])
    gap = upper["price"]-lower["price"]
    if end <= start or gap <= 2*tolerance:
        return None
    duration = bars[-1].time_ms+step-start
    up, down = upper["slope"]*duration, lower["slope"]*duration
    if abs(up-down) <= max(tolerance,.15*max(abs(up),abs(down))):
        drift = (up+down)/2
        kind = "range" if abs(drift)<=tolerance else ("ascending_channel" if drift>0 else "descending_channel")
    elif down-up > tolerance:
        kind = "rising_wedge" if up>tolerance else ("falling_wedge" if down < -tolerance else "triangle")
    else:
        return None
    close = bars[-1].close
    position = ("above" if close>upper["price"]+tolerance else "below" if close<lower["price"]-tolerance
                else "near_upper" if close>=upper["price"]-tolerance else "near_lower" if close<=lower["price"]+tolerance else "inside")
    return dict(kind=kind,position=position,upper=upper,lower=lower,as_of_ms=bars[-1].time_ms+step)


def pattern_context(series, now_ms):
    result = []
    for tf, step in (("4h",14400000),("1h",3600000),("15m",900000),("5m",300000)):
        bars = [b for b in series.get(tf,[]) if b.time_ms+step<=now_ms]
        seen = set()
        for radius in (6,2):
            pattern = detect(bars,step,radius)
            if pattern:
                key = (pattern["kind"],pattern["upper"]["anchors"][0][0],pattern["lower"]["anchors"][0][0])
                if key not in seen:
                    result.append(dict(timeframe=tf,**pattern))
                    seen.add(key)
    return result
