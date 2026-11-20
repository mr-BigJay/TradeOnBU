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
                # Check the zone's own *confirmed HTF distal edge* first.
                # Separate support/resistance/OB levels remain alternatives,
                # but remote 4H pivots no longer dominate the stop search.
                for inv in [zone] + [x for x in major if x["id"] != zone["id"]]:
                    same_zone = inv["id"] == zone["id"]
                    if not same_zone and inv["kind"] != (
                        "support" if sign == 1 else "resistance"
                    ) and inv["kind"] != "OB":
                        continue
                    edge = inv["low"] if sign == 1 else inv["high"]
                    zone_edge = zone["low"] if sign == 1 else zone["high"]
                    if not same_zone and sign * (zone_edge - edge) <= 0:
                        continue
                    proposal = dict(
                        strategy_id=sid, direction=direction,
                        zone_id=zone["id"], invalidation_id=inv["id"],
                        target_id=target["id"],
                        reason_fa="نمونهٔ هندسه برای ارزیابی تحلیل‌گر؛ تأیید ورود نیست.",
                    )
                    geometry = proposal_geometry(
                        direction, zone, inv, target, evidence, series, costs
                    )
                    try:
                        compile_plan(
                            proposal, evidence, series, costs,
                            observed_ms, observed_ms, seed=False
                        )
                    except ValueError as exc:
                        key = sid + ":" + str(exc)
                        rejected[key] += 1
                        # Record the *closest* rejected structural stop rather
                        # than the first arbitrarily far 4H stop in the catalog.
                        old = examples.get(key)
                        if old is None or geometry["stop_distance"] < old["stop_distance"]:
                            examples[key] = geometry | {
                                "zone_id": zone["id"],
                                "invalidation_id": inv["id"],
                                "target_id": target["id"],
                                "zone_timeframe": zone["timeframe"],
                                "invalidation_timeframe": inv["timeframe"],
                            }
                        continue
                    group.append(
                        {k: v for k, v in proposal.items() if k != "reason_fa"}
                        | geometry
                    )
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
    # Make it explicit whether the missing signal is due to no signed HTF
    # geometry versus a downstream WAIT/order gate. Counts are combinations.
    blocked = {}
    for sid in PROFILES:
        if not strategies.get(sid):
            continue
        valid = sum(counts.get(sid + ":" + direction, 0)
                    for direction in ("LONG", "SHORT"))
        if valid:
            blocked[sid] = {"status": "feasible_geometry_available", "valid_combinations": valid}
            continue
        relevant = {
            key.split(":", 1)[1]: value for key, value in rejected.items()
            if key.startswith(sid + ":")
        }
        blocked[sid] = {
            "status": "no_feasible_structural_geometry",
            "top_rejections": sorted(
                relevant.items(), key=lambda item: (-item[1], item[0])
            )[:5],
        }
    return dict(policy="nearest_confirmed_htf_invalidation_v2", confirmed=False,
                note="Candidate geometry only, NOT orders or fills. Invalidation may use the confirmed 1H/4H source zone distal edge with ATR floor or a separate nearby confirmed HTF level. Net fee-adjusted >=1.5R, stop width, obstacles and live-price checks remain enforced. A/C require a later genuine 5m trigger; B may rest a signed paper LIMIT before rejection, with a real later 1m touch for fill.",
                choices=choices, feasible_counts=dict(counts),
                rejection_counts=dict(rejected), rejection_examples=examples,
                strategy_diagnostics=blocked)
