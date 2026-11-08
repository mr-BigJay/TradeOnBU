"""Closed-candle, HTF-only shortlist for major watch-only support and resistance.

The executable 5m/15m evidence catalog is left untouched. A confirmed HTF
swing by itself is not classified as a major level: closed-candle reaction,
intact structure, and (for most 1h pivots) independent confluence are required.
These volatility-scaled thresholds are uncalibrated heuristics, not probabilities.
"""
from __future__ import annotations

import math

from app.services.ict_structure import find_swings
from app.services.verified_trade_plan import atr

POLICY_VERSION = "major_htf_levels_v1"
TIMEFRAMES = ("1h", "4h")


def _role(level):
    if level.get("kind") in ("support", "resistance"):
        return level["kind"]
    if level.get("kind") == "OB":
        return {"LONG": "support", "SHORT": "resistance"}.get(level.get("direction"))
    return None


def _geometry_ok(level):
    try:
        low, high, price = (float(level[key]) for key in ("low", "high", "price"))
    except (KeyError, TypeError, ValueError):
        return False
    return all(math.isfinite(x) and x > 0 for x in (low, high, price)) and low <= price <= high


def minimum_forecast_gap(series):
    """Full-band source-to-target clearance in USDT for a four-hour forecast."""
    frames = (series.get("1h") or [], series.get("4h") or [])
    if any(len(bars) < 30 for bars in frames):
        return math.inf
    atr1, atr4 = (atr(bars) for bars in frames)
    if not all(math.isfinite(x) and x > 0 for x in (atr1, atr4)):
        return math.inf
    return max(atr1, 0.45 * atr4)


def select_major_levels(evidence, series, *, per_side=4):
    """Evidence-bound, duplicate-collapsed HTF levels for forecast use only."""
    if not isinstance(evidence, list):
        return []
    bars = {tf: series.get(tf) or [] for tf in TIMEFRAMES}
    if any(len(candles) < 30 for candles in bars.values()):
        return []
    atrs = {tf: atr(candles) for tf, candles in bars.items()}
    atr1, atr4 = atrs["1h"], atrs["4h"]
    if not all(math.isfinite(v) and v > 0 for v in (atr1, atr4)):
        return []
    swings = {tf: find_swings(candles) for tf, candles in bars.items()}
    price_now = bars["1h"][-1].close
    confluence_tolerance = max(0.16 * atr4, 0.5 * atr1)

    candidates = []
    for zone in evidence:
        if not isinstance(zone, dict):
            continue
        tf = zone.get("timeframe")
        kind = zone.get("kind")
        role = _role(zone)
        if (tf not in TIMEFRAMES or role is None or not _geometry_ok(zone)
                or zone.get("status") not in ("fresh", "retested")):
            continue
        if zone["high"] - zone["low"] > max(0.55 * atr4, 1.8 * atr1):
            continue
        if abs(zone["price"] - price_now) > 8 * atr4:
            continue
        if kind != "OB":
            expected = "LOW" if kind == "support" else "HIGH"
            if not any(
                p.kind == expected and p.time_ms == zone.get("time_ms")
                and math.isclose(p.price, zone["price"], rel_tol=0, abs_tol=1e-6)
                for p in swings[tf]
            ):
                continue
        elif zone.get("direction") not in ("LONG", "SHORT"):
            continue
        confirmed = zone.get("confirmed_ms")
        if not isinstance(confirmed, int):
            continue
        later = [bar for bar in bars[tf] if bar.time_ms >= confirmed]
        if len(later) < 2:
            continue
        low, high = zone["low"], zone["high"]
        if role == "support":
            if any(bar.close < low for bar in later):
                continue
            reaction = max(0, max(bar.close for bar in later) - high)
        else:
            if any(bar.close > high for bar in later):
                continue
            reaction = max(0, low - min(bar.close for bar in later))
        required = 0.70 * atr4 if tf == "4h" else max(1.3 * atr1, 0.35 * atr4)
        if reaction < required:
            continue
        candidates.append({
            "id": zone["id"], "kind": kind, "role": role,
            "timeframe": tf, "time_ms": zone.get("time_ms"),
            "price": zone["price"], "low": low, "high": high,
            "status": zone["status"], "touch_count": zone.get("touch_count"),
            "reaction_atr4h": round(reaction / atr4, 2),
            "reaction_atr_source": round(reaction / atrs[tf], 2),
            "proof": "confirmed_unbroken_swing" if kind != "OB" else "displacement_break_order_block",
        })

    eligible = []
    for candidate in candidates:
        confirmations = [
            x for x in candidates if x["id"] != candidate["id"]
            and x["role"] == candidate["role"]
            and x["time_ms"] != candidate["time_ms"]
            and abs(x["price"] - candidate["price"]) <= confluence_tolerance
        ]
        # A standalone 1h swing needs a true 4h-scale reaction.
        if (candidate["timeframe"] == "1h" and not confirmations
                and candidate["reaction_atr4h"] < 0.85):
            continue
        item = dict(candidate)
        item["confluence_count"] = len(confirmations)
        item["confluence_ids"] = [x["id"] for x in confirmations[:5]]
        item["quality_score"] = round(
            (4 if item["timeframe"] == "4h" else 2)
            + min(3, item["reaction_atr4h"])
            + min(2, len(confirmations)), 2
        )
        eligible.append(item)

    eligible.sort(key=lambda x: (
        -x["quality_score"], 0 if x["timeframe"] == "4h" else 1,
        -x["reaction_atr4h"], abs(x["price"] - price_now), str(x["id"]),
    ))
    selected, side_counts = [], {"support": 0, "resistance": 0}
    for item in eligible:
        if side_counts[item["role"]] >= per_side:
            continue
        if any(
            previous["role"] == item["role"]
            and (
                item["low"] <= previous["high"] and previous["low"] <= item["high"]
                or abs(item["price"] - previous["price"]) <= confluence_tolerance
            )
            for previous in selected
        ):
            continue
        selected.append(item)
        side_counts[item["role"]] += 1
    return sorted(selected, key=lambda x: (
        x["role"], abs(x["price"] - price_now), 0 if x["timeframe"] == "4h" else 1,
    ))
