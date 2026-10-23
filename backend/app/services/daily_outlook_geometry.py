"""Deterministic, descriptive overlays. No forecast model or market-data calls."""
import math


def nearby_levels(levels, quote):
    """Candidate S/R from saved extrema; proximity alone is not a confirmed reaction."""
    result = {}
    for side, candidates in (
        ("support", sorted((v for v in levels if v["price"] < quote), key=lambda v: -v["price"])),
        ("resistance", sorted((v for v in levels if v["price"] > quote), key=lambda v: v["price"])),
    ):
        chosen = []
        for item in candidates:
            if all(abs(item["price"] - old["price"]) > quote * .0015 for old in chosen):
                chosen.append({**item, "code": ("S" if side == "support" else "R") + str(len(chosen)+1)})
            if len(chosen) == 3:
                break
        result[side] = chosen
    return result


def _fit(points):
    xm = sum(x for x, _ in points) / len(points)
    ym = sum(y for _, y in points) / len(points)
    denom = sum((x-xm)**2 for x, _ in points)
    slope = sum((x-xm)*(y-ym) for x, y in points)/denom if denom else 0
    return slope, ym-slope*xm


def pivot_channel(rows, anchor, step, quote):
    """Fit parallel bounds to confirmed 2-left/2-right pivots in last 60 bars.

    Thresholds are display heuristics, not calibrated probabilities or trading gates.
    All x values are days relative to the saved report timestamp.
    """
    rows = rows[-60:]
    unavailable = {"available": False, "reason": "insufficient_structure"}
    if len(rows) < 40:
        return unavailable
    if any(b[0]-a[0] != step*1000 for a, b in zip(rows, rows[1:])):
        return {"available": False, "reason": "history_gap"}
    xs = [(r[0]/1000-anchor.timestamp())/86400 for r in rows]
    tr = [max(b[2]-b[3], abs(b[2]-a[4]), abs(b[3]-a[4])) for a,b in zip(rows,rows[1:])]
    atr = sum(tr[-14:])/14
    if atr <= 0:
        return unavailable
    upper, lower = [], []
    for i in range(2, len(rows)-2):
        neighbours = rows[i-2:i] + rows[i+1:i+3]
        if rows[i][2] > max(b[2] for b in neighbours):
            upper.append((xs[i], rows[i][2]))
        if rows[i][3] < min(b[3] for b in neighbours):
            lower.append((xs[i], rows[i][3]))
    if min(len(upper),len(lower)) < 2:
        return unavailable
    span = xs[-1]-xs[0]
    if min(upper[-1][0]-upper[0][0], lower[-1][0]-lower[0][0]) < span/3:
        return unavailable
    su, _ = _fit(upper)
    sl, _ = _fit(lower)
    slope = (su+sl)/2
    top = sum(y-slope*x for x,y in upper)/len(upper)
    bottom = sum(y-slope*x for x,y in lower)/len(lower)
    width = top-bottom
    if width < atr or abs(su-sl)*span > max(atr, width*.35):
        return {"available": False, "reason": "non_parallel"}
    tolerance = atr*.35
    touches = [[x for x,y in points if abs(y-(slope*x+offset)) <= tolerance]
               for points,offset in ((upper,top),(lower,bottom))]
    if any(len(t)<2 or max(t)-min(t)<span/3 for t in touches):
        return unavailable
    coverage = sum(bottom+slope*x-tolerance <= r[4] <= top+slope*x+tolerance
                   for x,r in zip(xs,rows))/len(rows)
    if coverage < .85:
        return {"available": False, "reason": "poor_fit"}
    if not (bottom+slope*xs[-1]-tolerance <= rows[-1][4] <= top+slope*xs[-1]+tolerance
            and bottom-tolerance <= quote <= top+tolerance):
        return {"available": False, "reason": "broken_channel"}
    if min(bottom+slope*xs[0],bottom+slope*7) <= 0 or not all(math.isfinite(v) for v in (slope,top,bottom)):
        return unavailable
    direction = "range" if abs(slope)*span < atr else "bullish" if slope > 0 else "bearish"
    return {"available": True, "direction": direction, "slope_per_day": slope,
            "upper": top, "lower": bottom, "mid": (top+bottom)/2,
            "start_day": xs[0], "end_day": xs[-1], "bars":len(rows),
            "upper_touches":len(touches[0]), "lower_touches":len(touches[1]),
            "close_coverage":round(coverage,3), "timeframe":"4h" if step==14400 else "1d"}
