from copy import deepcopy
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as NS
import pytest
from app.domain.candles import Candle
from app.services import multi_setup_analyst as a
from app.services.analyst_execution import execution_plan, priority_key
from app.services.analyst_confirmation import recent_confirmations
from app.services.ict_confirmation_engine import _token_on_bar
from tests.test_multi_setup_analyst import fixture, proposal


@pytest.mark.parametrize('short',[False,True])
def test_stop_tracks_observed_extreme_and_does_not_inflate_risk(short):
    from app.services.verified_plan_risk import risk_margin
    s,c,e,n=fixture();p=a.compile_plan(proposal('ict_reversal'),e,s,c,n,n)
    contract=deepcopy(p['verified_trade_plan']);stamp=n//300000*300000-300000
    bars=[Candle(1021,1024,1014,1021,stamp)]
    if short:
        for key in ('entry','stop','target','invalidation'):contract[key]=2000-contract[key]
        contract['evidence']['obstacle']=2000-contract['evidence']['obstacle']
        contract['direction']='SHORT';bars=[Candle(979,986,976,979,stamp)]
    row=NS(direction=contract['direction'],structured_snapshot={'verified_trade_plan':contract},
           confirmation_state={'confirmation_timestamps':{'liquidity_sweep':stamp}})
    plan,error=execution_plan(row,c,bars,n)
    assert error is None
    assert plan['stop']>986 if short else plan['stop']<1014
    kwargs=dict(equity=1000,available=1000,entry=contract['entry'],direction=row.direction,
                maker=c.maker_fee_rate,taker=c.taker_fee_rate,slippage_bps=c.slippage_bps,leverage=3,cap=1000)
    assert risk_margin(**kwargs,stop=plan['stop'])<risk_margin(**kwargs,stop=contract['stop'])
    contract['evidence']['obstacle']=contract['entry']+(-1 if short else 1)
    assert execution_plan(row,c,bars,n)[1]=='execution_insufficient_reward_before_obstacle'


def test_bound_break_is_cross_of_named_pivot_not_rolling_range():
    structured={'verified_trade_plan':{'confirmation_reference':{'break_price':105,'liquidity_price':95}}}
    bars=[Candle(100,110,94,100,0),Candle(100,107,99,106,300000)]
    assert _token_on_bar('mss',direction='LONG',window=bars,zone_lo=98,zone_hi=101,structured=structured)
    bars[-2]=Candle(106,110,99,106,0)
    assert not _token_on_bar('mss',direction='LONG',window=bars,zone_lo=98,zone_hi=101,structured=structured)
    bars[-1]=Candle(100,101,96,100,300000)
    assert not _token_on_bar('liquidity_sweep',direction='LONG',window=bars,zone_lo=98,zone_hi=101,structured=structured)


@pytest.mark.asyncio
async def test_api_rank_survives_persistence_and_monitor_sort(session,monkeypatch):
    from app.models import AiAnalysisRun
    from app.services.strategy_candidates import persist_strategy_candidates
    monkeypatch.setenv('MULTI_SETUP_ANALYST_ENABLED','1')
    s,c,e,n=fixture();order=list(reversed(a.PROFILES))
    p=a.compile_response({'plans':[proposal(sid) for sid in order]},e,s,c,n,n,{sid:True for sid in order})
    run=AiAnalysisRun(trigger='manual',status='completed',structured=p);session.add(run);await session.flush()
    rows,meta=await persist_strategy_candidates(session,run_id=run.id,symbol='BTCUSDT',structured=p)
    assert list(rows)==order,meta
    shuffled=list(reversed(list(rows.values())))
    assert [r.structured_snapshot['strategy_id'] for r in sorted(shuffled,key=priority_key)]==order


@pytest.mark.asyncio
async def test_retry_one_transient_failure_after_backoff_only(session):
    from app.models import AiAnalysisRun
    now=datetime.now(timezone.utc).replace(hour=8,minute=10,second=0,microsecond=0);_,slot=await a.review_allowed(session,now)
    row=AiAnalysisRun(trigger='scheduled',status='failed',created_at=now,
        completed_at=now,structured={'analyst_review':{'slot_ms':slot,'retryable':True}})
    session.add(row);await session.flush()
    assert not (await a.review_allowed(session,now))[0]
    later=now+timedelta(minutes=3)
    assert (await a.review_allowed(session,later))[0]
    session.add(AiAnalysisRun(trigger='scheduled',status='failed',created_at=later,
        completed_at=later,structured={'analyst_review':{'slot_ms':slot,'retryable':True}}))
    await session.flush()
    assert not (await a.review_allowed(session,later+timedelta(minutes=3)))[0]


def test_catalog_preserves_ob_status_and_touch_metadata(monkeypatch):
    s,c,e,n=fixture()
    t=s['5m'][-3].time_ms
    monkeypatch.setattr(a,'market_zones',lambda series:{'support_resistance':[],'order_blocks':[
        dict(timeframe='5m',origin_ms=t-300000,confirmed_ms=t,low=1000,high=1100,direction='LONG',status='retested')]})
    ob=next(x for x in a.catalog(s,n) if x['kind']=='OB')
    assert ob['status']=='retested' and ob['touch_count']==2 and ob['confirmed_ms']==t+300000


def test_recent_proof_needs_known_reference_and_cannot_replay_a_fill():
    step=300000;end=30*step
    area=dict(time_ms=0,timeframe='5m',confirmed_ms=step)
    contract=dict(strategy_id='ict_reversal',direction='LONG',zone=[99,101],entry=100,
        invalidation=90,breakout_level=None,evidence={'zone':area,'invalidation':area},
        confirmation_reference={'liquidity_price':95,'break_price':105,'break_confirmed_ms':2*step})
    bars=[Candle(100,101,94,99,27*step),Candle(99,107,99,106,28*step),Candle(106,108,105,107,29*step)]
    seed=recent_confirmations(contract,bars,end)
    assert seed['confirmation_timestamps']=={'liquidity_sweep':27*step,'mss':28*step}
    assert 'entry_zone_touch' not in seed['confirmation_timestamps']
    bars[-1]=Candle(106,108,99,107,29*step)
    assert not recent_confirmations(contract,bars,end)
    contract['confirmation_reference']['break_confirmed_ms']=end
    assert not recent_confirmations(contract,bars[:2],end)


def test_read_only_audit_reports_failed_usage_and_observed_results(tmp_path):
    import sqlite3,json
    from app.analyst_audit import audit
    path=tmp_path/'audit.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE ai_analysis_runs (status TEXT,structured TEXT,created_at TEXT)')
        db.execute('CREATE TABLE ai_proposed_setups (last_rejection_reason TEXT,last_validation_result TEXT,created_at TEXT)')
        db.execute('CREATE TABLE sim_positions (public_id TEXT,closed_at TEXT,close_reason TEXT,net_pnl_usdt REAL,extra TEXT,status TEXT)')
        payload={'analyst_review':{'api_attempted':True},'_artifacts':{'inference':{'response_metadata':{'usage':{'output_tokens':12}}}}}
        db.execute('INSERT INTO ai_analysis_runs VALUES (?,?,?)',('failed',json.dumps(payload),'2026-11-07'))
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        result=audit(db,'2026-11-01')
        assert result['usage']['output_tokens']==12 and result['analysis']['api_attempts']==1
        assert result['performance_by_strategy']==[]


def test_performance_does_not_mix_previous_and_corrected_execution():
    from app.services.strategy_performance import summarize
    common=dict(closed_at=datetime.now(timezone.utc),close_reason='tp_hit',net_pnl_usdt=2)
    old=NS(public_id='old',extra={'verified_trade_plan':{'version':a.VERSION},'strategy_id':'ict_reversal'},**common)
    new=NS(public_id='new',extra={**old.extra,'execution_plan':{'version':'structure_bound_v2'}},**common)
    groups=summarize([old,new])
    assert len(groups)==2
    assert {g['execution_version'] for g in groups}=={'legacy_execution','structure_bound_v2'}


@pytest.mark.asyncio
async def test_report_fees_match_selected_closes_and_version_metrics(session):
    from app.models import SimPosition
    from app.services.position.report_service import build_report
    now=datetime.now(timezone.utc)
    for i,(days,pnl,fee,reason) in enumerate([(0,8,2,'tp_hit'),(2,-10,50,'sl_hit'),(0,-5,1,'sl_hit'),(0,99,9,'ambiguous_intrabar')]):
        session.add(SimPosition(public_id=f'audit-{i}',symbol='BTCUSDT',direction='LONG',status='closed',
            entry_price=100,stop_loss=90,take_profit=120,
            margin_usdt=10,leverage=3,entry_fee_usdt=fee,exit_fee_usdt=1,funding_fee_usdt=0,
            gross_pnl_usdt=pnl+fee+1,net_pnl_usdt=pnl,closed_at=now-timedelta(days=days),close_reason=reason,
            extra={'strategy_id':'ict_reversal','verified_trade_plan':{'version':a.VERSION}}))
    await session.flush()
    report=await build_report(session,period='custom',start=now-timedelta(hours=1),end=now+timedelta(seconds=1))
    assert report['fees']=={'opening_fees':3,'closing_fees':2,'funding_fees':0}
    metric=report['performance_by_strategy'][0]
    assert metric['valid_trades']==2 and metric['excluded_trades']==1
    assert metric['net_pnl_usdt']==3 and metric['realized_drawdown_usdt']==5
