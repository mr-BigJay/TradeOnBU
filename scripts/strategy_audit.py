#!/usr/bin/env python3
"""Read-only funnel/outcome summary. No network, inference calls, or order writes."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3


def obj(value):
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def audit(db, days):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None).isoformat(sep=" ")
    counts = defaultdict(Counter)
    for row in db.execute("SELECT structured FROM ai_analysis_runs WHERE completed_at >= ?", (cutoff,)):
        data = obj(row[0])
        for sid, result in data.get("_artifacts", {}).get("strategy_persistence", {}).items():
            counts[sid]["reported_runs"] += 1
            state = "created" if result.get("created") else "revised" if result.get("updated") else result.get("reason", "skipped")
            counts[sid][state] += 1
    setups = {}
    for row in db.execute("SELECT setup_id, structured_snapshot, lifecycle_state, last_rejection_reason, created_at FROM ai_proposed_setups"):
        sid = obj(row[1]).get("strategy_id", "legacy_unspecified")
        setups[row[0]] = sid
        if row[4] and row[4] >= cutoff:
            counts[sid]["setup_state:" + str(row[2])] += 1
            if row[3]:
                counts[sid]["latest_block:" + row[3]] += 1
    # Cohort uses position creation date; pending positions are never counted as losses.
    for row in db.execute("SELECT ai_setup_id, extra, status, net_pnl_usdt FROM sim_positions WHERE created_at >= ?", (cutoff,)):
        sid = obj(row[1]).get("strategy_id") or setups.get(row[0], "legacy_unspecified")
        counts[sid]["order_state:" + row[2]] += 1
        if row[2] in ("closed", "tp_hit", "sl_hit", "liquidated") and row[3] is not None:
            counts[sid]["closed_with_pnl"] += 1
            counts[sid]["net_pnl_usdt"] += row[3]
            counts[sid]["wins" if row[3] > 0 else "losses" if row[3] < 0 else "breakeven"] += 1
    return {"days": days, "note": "Observed cohorts, not a controlled backtest; old missing strategy labels remain unknown.", "strategies": dict(counts)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="/app/data/tradeonbu.db")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    if args.days <= 0:
        parser.error("days must be positive")
    with sqlite3.connect(Path(args.db).resolve().as_uri() + "?mode=ro", uri=True) as db:
        print(json.dumps(audit(db, args.days), ensure_ascii=False, indent=2))
