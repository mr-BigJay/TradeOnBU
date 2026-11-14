"""Price observations for report continuity; never infer position fills or P&L."""
from datetime import datetime


def price_progress(previous: dict, current: dict) -> dict:
    try:
        start = int(datetime.fromisoformat(previous["as_of"].replace("Z", "+00:00")).timestamp()*1000)
    except (KeyError, TypeError, ValueError, AttributeError):
        return {"status": "unavailable", "reason": "missing_report_timestamp"}
    rows = sorted((r for r in (current.get("candles") or {}).get("4h", []) if r[0] >= start), key=lambda r:r[0])
    if not rows:
        return {"status": "unavailable", "reason": "no_complete_post_report_bars"}
    ids = (previous.get("dominant") or {}).get("target_level_ids", [])
    levels = {level["id"]: level["price"] for level in previous.get("levels", [])}
    touches = []
    for key in ids:
        if key not in levels:
            continue
        price = levels[key]
        first = next((r for r in rows if r[3] <= price <= r[2]), None)
        touches.append({"level_id": key, "price": price, "touched_in_observed_bars": first is not None,
                        "first_touch_open_ms": first[0] if first else None})
    return {"status": "observed_closed_bars_only", "from_ms": rows[0][0],
            "through_ms": rows[-1][0]+14_400_000, "bar_count": len(rows),
            "unobserved_initial_ms": rows[0][0]-start,
            "high": max(r[2] for r in rows), "low": min(r[3] for r in rows),
            "last_close": rows[-1][4], "targets": touches,
            "execution_status": "not_tracked"}
