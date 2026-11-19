"""Read-only production funnel for ABC analyst -> setup -> order -> filled paper.

Run: python scripts/diagnose_paper_entry_funnel.py
No AI/exchange calls. No migrations, changes, or secrets printed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import sqlite3


def as_dict(raw):
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="/app/data/tradeonbu.db")
    parser.add_argument("--hours", type=int, default=48)
    args = parser.parse_args()
    if not 1 <= args.hours <= 168:
        parser.error("--hours must be between 1 and 168")

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    print(f"=== ABC / PAPER FUNNEL: LAST {args.hours} HOURS (UTC) ===")
    runs = list(db.execute("""
        SELECT id, started_at, trigger, status, structured
        FROM ai_analysis_runs WHERE started_at >= ?
        ORDER BY id DESC
    """, (cutoff,)))
    total = Counter()
    by_strategy = Counter()
    persistence_reasons = Counter()
    for row in runs:
        structured = as_dict(row["structured"])
        summary = as_dict(structured.get("analyst_summary"))
        if not summary:
            continue
        total["analysis_runs"] += 1
        total["proposed"] += int(summary.get("proposed") or 0)
        total["analyst_accepted"] += int(summary.get("accepted") or 0)
        for name, entry in as_dict(
            as_dict(structured.get("_artifacts")).get("strategy_persistence")
        ).items():
            details = as_dict(entry)
            if details.get("created") or details.get("updated"):
                by_strategy[name] += 1
            elif details.get("reason"):
                persistence_reasons[str(details["reason"])] += 1
    print("AI:", dict(total))
    print("SETUPS PERSISTED BY STRATEGY:", dict(by_strategy))
    print("NOT PERSISTED:", persistence_reasons.most_common(12))

    setups = list(db.execute("""
        SELECT id, created_at, direction, status, lifecycle_state,
               last_rejection_reason, last_validation_result,
               confirmation_state, structured_snapshot
        FROM ai_proposed_setups WHERE created_at >= ?
        ORDER BY id DESC
    """, (cutoff,)))
    by_state = Counter(str(r["lifecycle_state"]) for r in setups)
    reasons = Counter()
    for row in setups:
        v = as_dict(row["last_validation_result"])
        reason = (v.get("order_skipped_reason") or v.get("reason")
                  or row["last_rejection_reason"])
        if reason:
            reasons[str(reason)] += 1
    print("SETUP STATES:", dict(by_state))
    print("SETUP LAST BLOCKERS:", reasons.most_common(15))
    print("LAST 12 SETUPS:")
    for row in setups[:12]:
        data = as_dict(row["structured_snapshot"])
        conf = as_dict(row["confirmation_state"])
        validation = as_dict(row["last_validation_result"])
        print(
            f"  #{row['id']} {row['created_at']} "
            f"{data.get('strategy_id', 'legacy')} {row['direction']} "
            f"{row['status']}/{row['lifecycle_state']} "
            f"missing={conf.get('missing', [])} "
            f"block={validation.get('order_skipped_reason') or validation.get('reason') or row['last_rejection_reason']}"
        )

    positions = list(db.execute("""
        SELECT id, created_at, status, ai_setup_id, extra
        FROM sim_positions WHERE created_at >= ?
        ORDER BY id DESC
    """, (cutoff,)))
    states = Counter(str(r["status"]) for r in positions)
    cancelled = Counter()
    for row in positions:
        if row["status"] == "cancelled":
            extra = as_dict(row["extra"])
            cancelled[str(extra.get("auto_paper_cancel_reason") or "unspecified")] += 1
    print("PAPER ORDERS / FILLS:", dict(states))
    print("CANCEL REASONS:", cancelled.most_common(12))
    print("LAST 10 PAPER ORDERS:")
    for row in positions[:10]:
        print(f"  #{row['id']} {row['created_at']} {row['status']} setup={row['ai_setup_id']}")

    audits = list(db.execute("""
        SELECT event_type, reason_code FROM ict_setup_audit_events
        WHERE created_at >= ?
        ORDER BY id DESC
    """, (cutoff,)))
    events = Counter((r["event_type"], r["reason_code"]) for r in audits)
    print("MONITOR EVENTS:", events.most_common(15))
    db.close()


if __name__ == "__main__":
    main()
