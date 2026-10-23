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



def _major_pivots(rows, radius, atr):
    upper, lower = [], []
    for i in range(radius, len(rows)-radius):
        neighbours = rows[i-radius:i] + rows[i+1:i+radius+1]
        # Require prominence on both sides, in addition to local extrema.
        left, right = rows[i-radius:i], rows[i+1:i+radius+1]
        if (rows[i][2] > max(b[2] for b in neighbours)
                and min(rows[i][2]-min(b[3] for b in side) for side in (left,right)) >= atr):
            upper.append(i)
        if (rows[i][3] < min(b[3] for b in neighbours)
                and min(max(b[2] for b in side)-rows[i][3] for side in (left,right)) >= atr):
            lower.append(i)
    return upper, lower


def _window_candidates(rows, anchor, step):
    """Anchor a boundary to two major wicks; place its parallel at the opposite extreme."""
    xs = [(r[0]/1000-anchor.timestamp())/86400 for r in rows]
    span = xs[-1]-xs[0]
    trs = sorted(max(b[2]-b[3],abs(b[2]-a[4]),abs(b[3]-a[4])) for a,b in zip(rows,rows[1:]))
    # Median true range prevents a single impulse setting the entire tolerance.
    atr = trs[len(trs)//2]
    if atr <= 0:
        return
    tolerance = .3*atr
    for radius in (3, 5):
        upper_pivots, lower_pivots = _major_pivots(rows,radius,atr)
        if min(len(upper_pivots),len(lower_pivots)) < 2:
            continue
        for side, pivots in ((2,upper_pivots),(3,lower_pivots)):
            for a_pos, a in enumerate(pivots):
                for b in pivots[a_pos+1:]:
                    if xs[b]-xs[a] < span*.35:
                        continue
                    slope = (rows[b][side]-rows[a][side])/(xs[b]-xs[a])
                    offset = rows[a][side]-slope*xs[a]
                    top = offset if side == 2 else max(r[2]-slope*x for x,r in zip(xs,rows))
                    bottom = offset if side == 3 else min(r[3]-slope*x for x,r in zip(xs,rows))
                    width = top-bottom
                    if width < 2*atr or min(bottom+slope*xs[0],bottom+slope*7) <= 0:
                        continue
                    wick_coverage = sum(bottom+slope*x-tolerance <= r[3] and r[2] <= top+slope*x+tolerance
                                        for x,r in zip(xs,rows))/len(rows)
                    close_coverage = sum(bottom+slope*x-tolerance <= r[4] <= top+slope*x+tolerance
                                         for x,r in zip(xs,rows))/len(rows)
                    if wick_coverage < .95 or close_coverage < .98:
                        continue
                    touches = [[i for i in indexes if abs(rows[i][field]-(slope*xs[i]+bound)) <= tolerance]
                               for indexes,field,bound in ((upper_pivots,2,top),(lower_pivots,3,bottom))]
                    if any(len(t)<2 or xs[max(t)]-xs[min(t)] < span*.35 for t in touches):
                        continue
                    # Contact at both outer edges must represent substantial rotation.
                    rotation = (max(r[4]-slope*x for x,r in zip(xs,rows))
                                - min(r[4]-slope*x for x,r in zip(xs,rows)))/width
                    if rotation < .35:
                        continue
                    direction = "range" if abs(slope)*span < atr else "bullish" if slope > 0 else "bearish"
                    # Prefer sustained structures over a short, neat internal fit.
                    score = (3*len(rows)/120 + wick_coverage
                             + .15*min(len(touches[0])+len(touches[1]),8)
                             + .2*radius/5)
                    yield {"available":True, "direction":direction, "slope_per_day":slope,
                           "upper":top, "lower":bottom, "mid":(top+bottom)/2,
                           "start_day":xs[0], "end_day":xs[-1], "bars":len(rows),
                           "upper_touches":len(touches[0]), "lower_touches":len(touches[1]),
                           "close_coverage":round(close_coverage,3), "wick_coverage":round(wick_coverage,3),
                           "timeframe":"4h", "score":score, "pivot_radius":radius,
                           "anchor_side":"upper" if side==2 else "lower",
                           "anchor_times_ms":[rows[a][0],rows[b][0]], "method":"major_pivot_envelope_v2",
                           "start_time_ms":rows[0][0], "end_time_ms":rows[-1][0]+step*1000}


def pivot_channel(rows, anchor, step, quote):
    """Rank multi-window 4h channels; preserve old broken structures without forcing a fit."""
    if step != 14400:
        return {"available":False,"reason":"requires_4h"}
    rows = rows[-120:]
    if len(rows) < 40:
        return {"available":False,"reason":"insufficient_structure"}
    if any(b[0]-a[0] != step*1000 for a,b in zip(rows,rows[1:])):
        return {"available":False,"reason":"history_gap"}
    best = None
    count = 0
    # Include exact daily offsets plus the most recent endpoint.
    ends = sorted(set(range(40,len(rows)+1,6)) | {len(rows)},reverse=True)
    for end in ends:
        lengths = sorted({40,60,90,120,end} & set(range(40,end+1)),reverse=True)
        for length in lengths:
            window = rows[end-length:end]
            for candidate in _window_candidates(window,anchor,step):
                count += 1
                candidate["score"] += .6*end/len(rows)
                if best is None or candidate["score"] > best["score"]:
                    best = candidate
    if best is None:
        return {"available":False,"reason":"insufficient_structure","candidates_evaluated":count}
    best.update(historical=best["end_time_ms"] < rows[-1][0]+step*1000,
                position="above" if quote>best["upper"] else "below" if quote<best["lower"] else "inside",
                candidates_evaluated=count, score=round(best["score"],3))
    return best
