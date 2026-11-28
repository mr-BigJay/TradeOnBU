"""Prospective AI hourly playbooks tied to a known structural anchor."""
import math

LABELS = {"A":"برگشت", "B":"ادامهٔ موج", "C":"شکست و بازآزمایی"}
STRATEGIES = dict(A="ict_reversal", B="trend_continuation", C="breakout_retest")
PLAN_PROMPT = """
Additionally return hourly_watch, at most three prospective hourly reaction plans:
{strategy:"A"|"B"|"C",direction:"LONG"|"SHORT",anchor_ms:integer,zone_low:number,
zone_high:number,expect_fa:string,stop_reference:number,target:number}.
Use an exact confirmed 1h/4h candle open timestamp defining the wave. Preserve its anchor
across reviews of the same opportunity. Distinguish direction of broader channel from
tradable hourly leg. In a descending channel A may buy a confirmed reversal at support;
at overhead resistance C needs a real break and retest. After rejection, B may buy a
higher low in the 50-62 percent retracement of the named upswing. If HL fails, watch A
at the old low, or C short after its break and retest. Mirror for bearish legs. State the
actual expected hourly candle reaction and prerequisites for each branch prospectively.
First address the closest relevant active channel or triangle boundary in market.chart_patterns.
Use horizontal_zones (original role, break_ms, retest_ms and state) to follow a broken horizontal level across reviews. Describe the relevant break and later retest in Persian. Compare it with the nearest horizontal zone. Explain in Persian which is encountered first and why.
For A consider an hourly sweep and reclaim at that edge; for C consider a closed break followed
by a later retest of the same moving edge; for B identify the first opposing boundary before target.
These are alternatives conditional on price behavior. Do not force an entry or every branch.
For a plan tied to a sloping edge name its timeframe, kind and side in expect_fa, and describe
how the zone follows it. If reference stop and objective leave net RR below 1.5, identify
the branch as watch-only in expect_fa. Do not adjust either level just to pass the threshold.
Anchor stops beyond the observed structural swing with room for ordinary hourly volatility. Use the next meaningful structural destination. Never compress distances to manufacture RR. These are watch zones for hourly AI market execution. No 5m/15m confirmation or resting order.
"""
READ_PROMPT = """Review the latest CLOSED hourly BTC candle against ONLY the supplied pre-existing
plans, their expected reaction, previous review and paper outcomes. No new market analysis.
YOU judge zone interaction (including wick and reclaim), strategy-specific reaction,
branch prerequisites, structural stop, remaining room to target and net RR >=1.5 with costs.
Use horizontal_zones to distinguish intact, broken and subsequently retested horizontal levels. A zone state is evidence; you still judge the expected reaction. Use pattern_edges_at_close for the hourly value of any named moving boundary, and major_zones
for horizontal obstacles. Apply the moving boundary only to plans explicitly tied to it.
For C distinguish the close outside from a later retest; for B assess obstacles before target.
Ignore converged geometry. If a required moving edge is unavailable, WAIT on that branch.
Compare the stop distance to normal hourly ranges and place the stop beyond the structural swing. Keep the planned structural objective. If those levels leave insufficient room at market, WAIT. Use WAIT if no eligible plan or no reaction. Do not re-enter a consumed plan.
Return JSON {action:"WAIT"|"ENTER",plan_key:string,reason_fa:string,stop_loss:number,
take_profit:number}. ENTER means execute paper market immediately. Never invent fills."""

def watch_plans(rows, series):
    stamps = {b.time_ms for tf in ("1h","4h") for b in series.get(tf, [])}
    plans = {}
    for row in rows[:3] if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        strategy, direction, anchor = (row.get(k) for k in ("strategy","direction","anchor_ms"))
        if strategy not in tuple(LABELS) or direction not in ("LONG","SHORT") or type(anchor) != int or anchor not in stamps:
            continue
        try:
            low, high, stop, target = [float(row[k]) for k in ("zone_low","zone_high","stop_reference","target")]
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
        if not all(math.isfinite(v) and v>0 for v in (low,high,stop,target)) or low>high or not row.get("expect_fa"):
            continue
        key = f"{strategy}:{direction}:{anchor}"
        plans[key] = dict(strategy=strategy,direction=direction,zone_low=low,zone_high=high,
                          expect_fa=str(row["expect_fa"])[:700],stop_reference=stop,target=target)
    return plans



def pattern_edges_at_close(patterns, close_ms):
    """Project the frozen forecast geometry onto a closed hourly candle."""
    edges = []
    for pattern in patterns:
        try:
            values = {}
            for side in ("upper", "lower"):
                edge = pattern[side]
                stamp, anchor = edge["anchors"][0]
                if close_ms < stamp:
                    break
                values[side] = anchor + (close_ms - stamp) * edge["slope"]
            if (len(values) == 2 and all(math.isfinite(v) for v in values.values())
                    and values["upper"] > values["lower"] > 0):
                edges.append(dict(kind=pattern["kind"], timeframe=pattern["timeframe"], **values))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return edges
