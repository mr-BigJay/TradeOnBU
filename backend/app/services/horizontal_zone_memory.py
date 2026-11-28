"""Replay closed hourly candles against previously published horizontal zones."""
HOUR = 3_600_000


def advance(zones, bars, close_ms):
    updated = {}
    for key, saved in zones.items():
        zone = dict(saved)
        support = zone["role"] == "support"
        for bar in bars:
            stamp = bar.time_ms + HOUR
            if not zone["as_of_ms"] < stamp <= close_ms:
                continue
            zone["as_of_ms"] = stamp
            outside = bar.close < zone["low"] if support else bar.close > zone["high"]
            if zone["state"] == "intact":
                if outside:
                    zone.update(state="broken", break_ms=stamp)
            elif not outside:
                zone.update(state="intact", break_ms=None, retest_ms=None)
            elif zone["state"] == "broken" and bar.high >= zone["low"] and bar.low <= zone["high"]:
                zone.update(state="retested", retest_ms=stamp)
        updated[key] = zone
    return updated


def snapshot(previous, selected, bars, now_ms):
    old = (previous or {}).get("analyst_summary") or {}
    levels = old.get("horizontal_zones")
    if levels is None:
        # Bootstrap the first review after upgrade from its published snapshot.
        prior_close = (old.get("market_scenario") or {}).get("as_of_ms")
        levels = _new(old.get("outlook_major_levels") or [], prior_close) if prior_close else {}
    old_levels = advance(levels, bars, now_ms)
    current = _new(selected, now_ms // HOUR * HOUR)
    # A broken support is absent from the unbroken-level catalog. Carry it
    # through the two-day intraday horizon to observe its retest from below.
    return {**current, **{key: zone for key, zone in old_levels.items()
                          if key in current or (zone.get("break_ms") or 0) >= now_ms - 48*HOUR}}


def _new(selected, stamp):
    return {f"{z['timeframe']}:{z['role']}:{z['time_ms']}:{z['kind']}":
            dict(role=z["role"], low=z["low"], high=z["high"], state="intact",
                 as_of_ms=stamp, break_ms=None, retest_ms=None)
            for z in selected if z.get("time_ms") is not None}
