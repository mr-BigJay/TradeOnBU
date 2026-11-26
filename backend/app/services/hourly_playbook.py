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
These are watch zones for hourly AI market execution. No 5m/15m confirmation or resting order.
"""
READ_PROMPT = """Review the latest CLOSED hourly BTC candle against ONLY the supplied pre-existing
plans, their expected reaction, previous review and paper outcomes. No new market analysis.
YOU judge zone interaction (including wick and reclaim), strategy-specific reaction,
branch prerequisites, structural stop, remaining room to target and net RR >=1.5 with costs.
Use WAIT if no eligible plan or no reaction. Do not re-enter a consumed plan.
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


