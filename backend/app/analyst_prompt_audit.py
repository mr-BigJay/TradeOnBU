"""Read-only audit for the first post-deploy 4h analyst result.

Usage:
  python -m app.analyst_prompt_audit --after-run 1158
This does not invoke the provider or affect Paper/Live orders.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def _obj(raw):
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def inspect(db, *, after_run=1158):
    rows = db.execute(
        """SELECT id, created_at, status, model_slug, structured, report_fa
           FROM ai_analysis_runs WHERE id > ? ORDER BY id DESC LIMIT 200""",
        (after_run,),
    ).fetchall()
    for row in rows:
        data = _obj(row["structured"])
        review = data.get("analyst_review") or {}
        if not review.get("api_attempted"):
            continue
        summary = data.get("analyst_summary") or {}
        artifacts = data.get("_artifacts") or {}
        usage = (((artifacts.get("inference") or {})
                  .get("response_metadata") or {}).get("usage") or {})
        payload = (review.get("payload_sizes")
                   or artifacts.get("analyst_payload") or {})
        sections = payload.get("sections") or {}
        total_chars = payload.get("after_chars") or 0
        shares = {
            name: {
                "json_chars": detail.get("after_chars"),
                "pct_of_user_payload": (
                    round(100 * detail.get("after_chars", 0) / total_chars, 1)
                    if total_chars else None
                ),
            }
            for name, detail in sections.items() if isinstance(detail, dict)
        }
        result = {
            "run_id": row["id"],
            "created_at": row["created_at"],
            "status": row["status"],
            "model": row["model_slug"],
            "usage": usage,
            "previous_run_1158_input_tokens": 52757,
            "change_input_tokens_vs_1158_pct": (
                round(100 * (usage["input_tokens"] / 52757 - 1), 2)
                if isinstance(usage.get("input_tokens"), (int, float)) else None
            ),
            "total_request_chars": payload.get("request_text_chars"),
            "instructions_chars": payload.get("instructions_chars"),
            "input_compaction_pct": payload.get("reduction_pct"),
            "candle_counts_before": payload.get("candle_counts"),
            "candle_counts_sent": payload.get("sent_candle_counts"),
            "sections": shares,
            "subsections": payload.get("breakdown"),
            "forecast_watchlist": summary.get("forecast_scenarios"),
            "forecast_rejections": summary.get("forecast_rejections"),
            "plan_proposed": summary.get("proposed"),
            "plan_accepted": summary.get("accepted"),
            "plan_rejections": summary.get("rejected_plans"),
            "execution_regime": summary.get("execution_regime"),
            "report_excerpt": (row["report_fa"] or "")[:2500],
            "limits": ("Historical 1158 per-section token attribution unavailable. "
                       "Current sections are actual JSON character shares, not provider token shares."),
        }
        return result
    return {"status": "NO_NEW_ANALYST_RUN_AFTER_" + str(after_run)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--after-run", type=int, default=1158)
    parser.add_argument("--db", default="/app/data/tradeonbu.db")
    args = parser.parse_args()
    with sqlite3.connect(Path(args.db).resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        print(json.dumps(inspect(db, after_run=args.after_run),
                         ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
