"""Closed-pivot trendlines for C. A fixed limit expires before it leaves the retest band."""
from app.services.ict_structure import find_swings


def line_price(zone, timestamp):
    return zone["anchor_price"] + (timestamp - zone["time_ms"]) * zone["slope_per_ms"]


def boundaries(bars, step, now_ms, width):
    pivots = find_swings(bars)
    lines = {}
    for kind, direction, sign in (("HIGH", "LONG", 1), ("LOW", "SHORT", -1)):
        points = [p for p in pivots if p.kind == kind]
        if len(points) < 2:
            continue
        first, last = points[-2:]
        slope = (last.price-first.price)/(last.time_ms-first.time_ms)
        if sign*slope >= 0 or last.index-first.index < 3:
            continue
        zone = dict(anchor_price=last.price, time_ms=last.time_ms, slope_per_ms=slope,
                    first_ms=first.time_ms, first_price=first.price, pattern="trendline",
                    confirmed_ms=last.time_ms+3*step)
        if any(sign*(b.close-line_price(zone,b.time_ms+step)) > width
               for b in bars[first.index:last.index+1]):
            continue
        if any(sign*(b.close-line_price(zone,b.time_ms+step)) > width
               for b in bars[last.index+1:] if b.time_ms+step < now_ms-900000):
            continue
        zone["price"] = line_price(zone, now_ms)
        zone["valid_until_ms"] = now_ms + int(max(0,width-.1)/abs(slope))
        if zone["price"] > width and zone["valid_until_ms"] > now_ms+300000:
            lines[direction] = zone
    if len(lines) == 2:
        upper, lower = lines["LONG"], lines["SHORT"]
        if max(upper["first_ms"], lower["first_ms"]) < min(upper["time_ms"], lower["time_ms"]) and upper["price"] > lower["price"]:
            apex = now_ms + int((upper["price"]-lower["price"])/(lower["slope_per_ms"]-upper["slope_per_ms"]))
            for zone in lines.values():
                zone.update(pattern="triangle", valid_until_ms=min(zone["valid_until_ms"],apex))
    return lines


def chart_zone(plan, timestamps=None):
    if not plan.get("breakout_zone_confirmation"):
        return {}
    zone = plan["evidence"]["zone"]
    stamps = timestamps or (plan.get("seed_confirmations") or {}).get("confirmation_timestamps", {})
    return dict(zone_low=zone["low"], zone_high=zone["high"], origin_ms=zone["time_ms"],
                origin_price=zone.get("anchor_price", zone["price"]), timeframe=zone["timeframe"],
                last_touch_ms=zone.get("last_touch_ms"), breakout_confirmed="breakout_close" in stamps,
                pattern_zone=zone if zone.get("pattern") else None)
