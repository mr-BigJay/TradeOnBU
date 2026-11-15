"""Non-binding geometry hints; final proposals still use the full compiler."""
from collections import Counter

from app.services.multi_setup_analyst import PROFILES, compile_plan, proposal_geometry


def build_execution_feasibility(evidence, series, costs, observed_ms, strategies):
    major = [x for x in evidence if x["timeframe"] in ("1h", "4h")
             and x["kind"] in ("support", "resistance", "OB")]
    counts, rejected, examples = Counter(), Counter(), {}
    choices = []
    for sid in PROFILES:
        if not strategies.get(sid):
            continue
        for direction, sign in (("LONG", 1), ("SHORT", -1)):
            group = []
            for zone in major:
                zone_kind = ("resistance" if sign == 1 else "support") if sid == "breakout_retest" else (
                    "support" if sign == 1 else "resistance")
                if zone["kind"] != zone_kind and not (sid != "breakout_retest" and zone["kind"] == "OB"):
                    continue
                if zone.get("direction") and zone["direction"] != direction:
                    continue
                entry = round((zone["low"] + zone["high"])/2, 1)
                targets = [x for x in major if x["kind"] == ("resistance" if sign == 1 else "support")
                           and sign*(x["price"]-entry) > 0]
                if not targets:
                    rejected[sid + ":no_structural_target"] += 1
                    continue
                target = min(targets, key=lambda x: sign*x["price"])
                for inv in major:
                    if inv["kind"] != ("support" if sign == 1 else "resistance") and inv["kind"] != "OB":
                        continue
                    # Impossible same/inner levels never reach the compiler.
                    edge = inv["low"] if sign == 1 else inv["high"]
                    zone_edge = zone["low"] if sign == 1 else zone["high"]
                    if sign*(zone_edge-edge) <= 0:
                        continue
                    proposal = dict(strategy_id=sid, direction=direction, zone_id=zone["id"],
                        invalidation_id=inv["id"], target_id=target["id"],
                        reason_fa="نمونهٔ هندسه برای ارزیابی تحلیل‌گر؛ تأیید ورود نیست.")
                    try:
                        compile_plan(proposal, evidence, series, costs,
                                            observed_ms, observed_ms, seed=False)
                    except ValueError as exc:
                        key = sid + ":" + str(exc)
                        rejected[key] += 1
                        if key not in examples:
                            examples[key] = proposal_geometry(
                                direction, zone, inv, target, evidence, series, costs)
                        continue
                    geometry = proposal_geometry(direction, zone, inv, target, evidence, series, costs)
                    group.append({k:v for k,v in proposal.items() if k != "reason_fa"} | geometry)
            counts[sid + ":" + direction] = len(group)
            # Keep prompt size bounded, diversify entry areas. This is NOT an allowlist.
            group.sort(key=lambda p: (abs(p["entry"]-series["1m"][-1].close), p["stop_distance"]))
            seen_zones = set()
            for item in group:
                if item["zone_id"] in seen_zones:
                    continue
                seen_zones.add(item["zone_id"])
                choices.append(item)
                if len(seen_zones) == 4:
                    break
    return dict(policy="advisory_geometry_v1", confirmed=False,
                note="Examples only, not an allowlist or orders. Other catalog combinations remain allowed. "
                     "Counts are geometry combinations, not distinct signals; all need closed-5m proof and later fill.",
                choices=choices, feasible_counts=dict(counts), rejection_counts=dict(rejected),
                rejection_examples=examples)
