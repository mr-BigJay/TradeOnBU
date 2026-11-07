"""Read-only production evidence: python -m app.analyst_audit --days 7."""
import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from pathlib import Path
from app.services.strategy_performance import summarize


def obj(raw):
    try:
        value=json.loads(raw or '{}')
        return value if isinstance(value,dict) else {}
    except (ValueError,TypeError):return {}


def audit(db, since):
    counts=Counter();rejections=Counter();usage=Counter()
    for row in db.execute('SELECT status,structured FROM ai_analysis_runs WHERE created_at>=?',(since,)):
        data=obj(row['structured']);review=data.get('analyst_review') or {}
        if not review:continue
        counts['runs']+=1;counts['status:'+row['status']]+=1
        counts['api_attempts']+=bool(review.get('api_attempted'))
        summary=data.get('analyst_summary') or {}
        counts['proposed']+=summary.get('proposed',0);counts['accepted']+=summary.get('accepted',0)
        rejections.update(summary.get('rejections') or [])
        metadata=((data.get('_artifacts') or {}).get('inference') or {}).get('response_metadata') or {}
        tokens=metadata.get('usage') or {}
        counts['attempts_without_usage']+=bool(review.get('api_attempted') and not tokens)
        for key in ('input_tokens','output_tokens','total_tokens'):usage[key]+=tokens.get(key,0) or 0
    blocked=Counter()
    for row in db.execute('SELECT last_rejection_reason,last_validation_result FROM ai_proposed_setups WHERE created_at>=?',(since,)):
        reason=obj(row['last_validation_result']).get('order_skipped_reason') or row['last_rejection_reason']
        if reason:blocked[reason]+=1
    trades=[]
    for row in db.execute("SELECT public_id,closed_at,close_reason,net_pnl_usdt,extra FROM sim_positions WHERE closed_at>=? AND status IN ('closed','tp_hit','sl_hit','liquidated')",(since,)):
        item=dict(row);item['extra']=obj(item['extra']);item['closed_at']=datetime.fromisoformat(item['closed_at'])
        trades.append(SimpleNamespace(**item))
    return dict(since_utc=since,analysis=dict(counts),usage=dict(usage),proposal_rejections=dict(rejections),
                latest_setup_block_reasons=dict(blocked),performance_by_strategy=summarize(trades),
                method='Recorded Paper outcomes only, grouped by engine version; not a historical AI backtest.')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--days',type=int,default=7)
    parser.add_argument('--db',default='/app/data/tradeonbu.db');args=parser.parse_args()
    if args.days<=0:parser.error('--days must be positive')
    since=(datetime.now(timezone.utc)-timedelta(days=args.days)).strftime('%Y-%m-%d %H:%M:%S')
    with sqlite3.connect(Path(args.db).resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row;db.execute('BEGIN')
        print(json.dumps(audit(db,since),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
