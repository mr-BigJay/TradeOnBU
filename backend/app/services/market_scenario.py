"""Persistent four-hour market context, independent of intraday order lifetimes."""
import json
from datetime import datetime, timezone
from pathlib import Path

from app.services.daily_outlook_data import aggregate_complete
from app.services.verified_trade_plan import atr, trend

STEP = 14_400_000


def event_risk(now_ms, path=Path("/app/data/market_events.json")):
    """Operator-verified calendar or news windows in UTC epoch milliseconds."""
    try:
        rows = json.loads(path.read_text())
        if not isinstance(rows, list):
            raise ValueError
        for row in rows:
            if (isinstance(row, dict) and isinstance(row.get("source"), str)
                    and row["source"].startswith("https://") and isinstance(row.get("title"), str)
                    and isinstance(row.get("start_ms"), int) and isinstance(row.get("end_ms"), int)
                    and row["start_ms"] <= now_ms < row["end_ms"]):
                return {"status": "pause", "title": row["title"][:120], "source": row["source"]}
        return {"status": "no_listed_event"}
    except (OSError, ValueError):
        return {"status": "unavailable"}


def broad_channel(bars, swings):
    """Fit parallel boundaries using separated, confirmed reactions on both sides."""
    tolerance = atr(bars) * .5
    if not tolerance:
        return None
    best, score = None, (0, 0)
    for side, kind, sign in (("upper", "HIGH", 1), ("lower", "LOW", -1)):
        points = [p for p in swings if p.kind == kind]
        for i, a in enumerate(points):
            for b in points[i+1:]:
                span = b.index - a.index
                if span < 24 or len(bars)-1-b.index > span or sign*(b.price-a.price) >= -tolerance:
                    continue
                slope = (b.price-a.price)/(b.time_ms-a.time_ms)
                def line(t):
                    return a.price + slope*(t-a.time_ms)
                others = [p for p in swings if p.kind != kind and p.index >= a.index]
                if len(others) < 2:
                    continue
                width = max(sign*(line(p.time_ms)-p.price) for p in others)
                touches = [p for p in others if abs(sign*(line(p.time_ms)-p.price)-width) <= tolerance]
                if width <= 2*tolerance or touches[-1].index-touches[0].index < 6:
                    continue
                if (span, len(touches)) <= score:
                    continue
                if any(not -tolerance <= sign*(line(c.time_ms)-c.close) <= width+tolerance for c in bars[a.index:]):
                    continue
                def edge(first, last, offset):
                    return {"anchors": [[p.time_ms,line(p.time_ms)+offset] for p in (first,last)],
                            "slope": slope,"price":line(bars[-1].time_ms+STEP)+offset}
                opposite = "lower" if side == "upper" else "upper"
                stamp = bars[-1].time_ms+STEP
                best = {side: edge(a,b,0), opposite: edge(touches[0],touches[-1],-sign*width),
                        "kind": "descending_channel" if sign == 1 else "ascending_channel",
                        "timeframe":"4h", "formed_ms":stamp, "as_of_ms":stamp, "position":"inside"}
                score = span,len(touches)
    return best


def review(previous, bars, swings, channel, now_ms):
    daily = trend(aggregate_complete(bars, days=1, now_ms=now_ms))
    old = (previous or {}).get("analyst_summary", {}).get("market_scenario") or {}
    direction = old.get("direction")
    level = old.get("level")
    breached = None
    if direction and level:
        for bar in bars:
            if bar.time_ms+STEP <= old["as_of_ms"]:
                continue
            if (bar.close-level)*(1 if direction=="SHORT" else -1)>0:
                breached = bar
                break
    event = event_risk(now_ms)
    scenario = dict(daily=daily, event=event, as_of_ms=bars[-1].time_ms+STEP if bars else 0)
    if direction and level and breached is None:
        return {**old, **scenario, "status":"continued", "broken":None}
    bias = daily if daily in ("bullish","bearish") else trend(bars)
    if channel:
        bias = "bearish" if channel["kind"]=="descending_channel" else "bullish"
    direction = {"bearish":"SHORT","bullish":"LONG"}.get(bias)
    sign = 1 if direction=="SHORT" else -1
    levels = [p.price for p in swings if p.kind==("HIGH" if sign==1 else "LOW")
              and sign*(p.price-bars[-1].close)>=atr(bars)]
    active = direction and levels
    return {**scenario,"direction":direction if active else None,"level":levels[-1] if active else None,
            "status":"new" if active else "range",
            "broken": {"level":level,"close":breached.close,"time":datetime.fromtimestamp(
                (breached.time_ms+STEP)/1000,timezone.utc).strftime("%Y-%m-%d %H:%M UTC")} if breached else None}
