"""Synthetic integration fixtures, not historical performance evidence."""
import json
import math
import random
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.domain.candles import Candle
from app.services import verified_trade_plan as v
from app.services.verified_plan_risk import risk_margin


def fixture(now=3_600_000_000):
    cost=SimpleNamespace(default_margin_usdt=50,default_leverage=3,maker_fee_rate=.0002,
        taker_fee_rate=.0005,slippage_bps=1,default_tp_behavior='market')
    now=now//3_600_000*3_600_000
    higher={tf:[Candle(1000,1040 if i%8==0 else 1003,960 if i%8==4 else 997,1000,
        now//st*st-(40-i)*st) for i in range(40)] for tf,st in [('1h',3600000),('15m',900000)]}
    rng=random.Random(803);bars=[];price=1000
    for i in range(60):
        o=price;c=o+rng.gauss(0,1.5);h=max(o,c)+rng.random();lo=min(o,c)-rng.random()
        bars.append(Candle(o,h,lo,c,now-(60-i)*v.STEP));price=c
    return {**higher,'5m':bars},cost,now,price


def candidate():
    series,cost,now,live=fixture()
    cs,diag=v.build_candidates(series,cost,now,live)
    assert cs,diag
    return cs[0],series,cost,now,live


def test_real_detector_proves_structure_and_cost_adjusted_target():
    c,series,cost,now,live=candidate()
    ev=c['evidence'];assert ev['sweep_ms']<ev['break_ms']<ev['fvg_ms']
    assert 1<=ev['stop_atr']<=3
    assert c['stop']>ev['sweep_extreme']>c['entry']>c['target']>ev['obstacle']
    from app.services.execution_model_sim import auto_paper_rr_from_settings
    rr=auto_paper_rr_from_settings(cost,direction=c['direction'],entry=c['entry'],sl=c['stop'],tp=c['target'],min_net_rr=1.4)
    assert rr.acceptable


@pytest.mark.parametrize('bad', ['future','gap','duplicate','nan','open_outside','stale'])
def test_corrupt_or_unclosed_history_never_produces_plan(bad):
    c,s,cost,now,live=candidate();bars=s['5m']
    if bad=='future':bars.append(replace(bars[-1],time_ms=now))
    elif bad=='gap':del bars[-8]
    elif bad=='duplicate':bars.insert(-8,bars[-8])
    elif bad=='nan':bars[0]=replace(bars[0],high=float('nan'))
    elif bad=='open_outside':bars[0]=replace(bars[0],open=bars[0].high+1)
    else:bars.pop()
    cs,reason=v.build_candidates(s,cost,now,live)
    assert not cs and reason=={'invalid_or_stale_5m':1}


def test_used_zone_is_not_recycled():
    c,s,cost,now,live=candidate()
    s['5m'][-1]=replace(s['5m'][-1],high=c['zone'][1]+1)
    cs,reasons=v.build_candidates(s,cost,now,live)
    assert not cs
    assert reasons['already_retested']


def test_model_cannot_supply_prices_or_unknown_candidate():
    c,s,cost,now,live=candidate()
    raw={'approve':True,'candidate_id':c['candidate_id'],'entry_price':1,'stop_loss':2,'strategy_candidates':{'x':{}},'reason_fa':'تأیید'}
    plan=v.apply_review(raw,[c],now+1000)
    assert plan['entry_price']==c['entry'] and plan['stop_loss']==c['stop']
    assert not plan['strategy_candidates'] and v.plan_error(plan,now+1000) is None
    raw['candidate_id']='made-up'
    assert v.apply_review(raw,[c],now)['setup_mode']=='flat'


@pytest.mark.parametrize('review',[{},None,{'approve':'true'},{'approve':False}])
def test_review_fails_closed(review):
    c,*_=candidate()
    assert v.apply_review(review,[c],c['as_of_ms'])['setup_mode']=='flat'


def test_late_model_response_rejected_and_binding_detects_mutation():
    c,s,cost,now,live=candidate();review={'approve':True,'candidate_id':c['candidate_id']}
    assert v.apply_review(review,[c],now+120001)['setup_mode']=='flat'
    p=v.apply_review(review,[c],now)
    p['stop_loss']+=1
    assert v.plan_error(p)=='verified_plan_levels_changed'
    p['verified_trade_plan']['stop']+=1
    assert v.plan_error(p)=='verified_plan_contract_changed'


def test_long_mirror_has_symmetric_proof():
    _,s,cost,now,live=candidate()
    s={tf:[Candle(2000-b.open,2000-b.low,2000-b.high,2000-b.close,b.time_ms) for b in bars] for tf,bars in s.items()}
    cs,_=v.build_candidates(s,cost,now,2000-live)
    assert cs and cs[0]['direction']=='LONG'
    assert cs[0]['stop']<cs[0]['invalidation']<cs[0]['entry']<cs[0]['target']


def test_risk_budget_includes_fees_and_wider_stop_reduces_size():
    args=dict(equity=1000,available=1000,entry=1000,stop=990,direction='LONG',maker=.0002,taker=.0005,slippage_bps=1,leverage=3,cap=1000)
    a=risk_margin(**args);args['stop']=980;b=risk_margin(**args)
    assert 0<b<a
    exit_price=990*(1-.0001);qty=a*3/1000
    assert qty*(1000-exit_price+1000*.0002+exit_price*.0005)<=2.5


@pytest.mark.asyncio
async def test_cadence_and_review_budget(monkeypatch,session):
    from app.services.ai_analysis_scheduler import _effective_interval_minutes
    from app.models import AiAnalysisRun
    monkeypatch.setenv('VERIFIED_PLANS_ENABLED','1')
    assert _effective_interval_minutes(SimpleNamespace(interval_minutes=60))==5
    c,*_=candidate();now=datetime.now(timezone.utc)
    for i in range(4):
        session.add(AiAnalysisRun(trigger='scheduled',status='completed',created_at=now,
            structured={'verified_plan_review':{'api_attempted':True,'opportunity_ids':[str(i)]}}))
    await session.flush()
    assert await v.unreviewed_candidates(session,[c],now)==([], 'daily_api_review_limit')


def test_token_budget_keeps_minimum_history():
    from app.services.ai_market_payload import _apply_token_budget,MarketContextError
    p={'timeframes':{tf:{'candles':[{'n':i} for i in range(30)]} for tf in ('1m','5m','15m','1h','4h')}}
    out=_apply_token_budget(deepcopy(p),1600)
    assert all(len(x['candles'])>=20 for x in out['timeframes'].values())
    with pytest.raises(MarketContextError):_apply_token_budget(deepcopy(p),10)

@pytest.mark.asyncio
async def test_contract_persists_exact_levels_and_preentry_proof(session, monkeypatch):
    from app.models import AiAnalysisRun
    from app.services.strategy_candidates import persist_strategy_candidates
    from app.services.conditional_revision import objective_confirmations_satisfied_for_order
    now=datetime.now(timezone.utc)
    series,cost,boundary,live=fixture(int(now.timestamp()*1000))
    cs,diag=v.build_candidates(series,cost,boundary,live)
    # Shift fixture to the last closed 5m boundary so wall-clock expiry remains valid.
    offset=int(now.timestamp()*1000)//v.STEP*v.STEP-boundary
    series={tf:[replace(b,time_ms=b.time_ms+offset) for b in bars] for tf,bars in series.items()}
    # Contract persistence uses already verified evidence; test current-clock timestamps.
    c=cs[0]
    for key in ('formed_ms','expires_ms','as_of_ms'):c[key]+=offset
    for key in ('sweep_ms','swept_pivot_ms','break_ms','broken_pivot_ms','fvg_ms','poi_origin_ms'):
        if c['evidence'].get(key) is not None:c['evidence'][key]+=offset
    c['evidence']['fvg_origin_ms']=[t+offset for t in c['evidence']['fvg_origin_ms']]
    import hashlib
    binding={k:val for k,val in c.items() if k not in ('candidate_id','as_of_ms','approved_at_ms')}
    c['candidate_id']=hashlib.sha256(json.dumps(binding,sort_keys=True).encode()).hexdigest()[:24]
    p=v.apply_review({'approve':True,'candidate_id':c['candidate_id']},[c],c['as_of_ms'])
    run=AiAnalysisRun(trigger='manual',status='completed',structured=p)
    session.add(run);await session.flush()
    monkeypatch.setenv('VERIFIED_PLANS_ENABLED','1')
    rows,meta=await persist_strategy_candidates(session,run_id=run.id,symbol='BTCUSDT',structured=p)
    assert rows,meta
    row=rows['ict_reversal']
    assert row.entry_price==c['entry']
    assert int(row.expires_at.timestamp()*1000)==c['expires_ms']
    assert objective_confirmations_satisfied_for_order(row,allow_pre_entry_without_touch=True)==(True,None)
    assert objective_confirmations_satisfied_for_order(row)[0] is False

@pytest.mark.asyncio
@pytest.mark.parametrize('poi_kind',['FVG','OB'])
async def test_verified_plan_reaches_real_paper_order_service(session,monkeypatch,poi_kind):
    from app.models import AiAnalysisRun,SimPosition
    from app.services.strategy_candidates import persist_strategy_candidates
    from app.services.paper_auto_trading import maybe_auto_open_paper_position
    from app.services import paper_auto_trading as auto
    from app.services.position.settings_service import get_or_create_settings
    from app.services.position.wallet_service import append_ledger,LEDGER_INITIAL
    from app.services.market_data import MarketDataService,KlineBundle
    from app.services.conditional_setup import apply_conditional_plan_to_setup_row
    from sqlalchemy import select
    import hashlib
    c,series,_,base,live=candidate()
    if poi_kind=='OB':
        from tests.test_poi_context import ob_fixture
        series,cost,base,live=ob_fixture()
        c=v.build_candidates(series,cost,base,live)[0][0]
    boundary=int(datetime.now(timezone.utc).timestamp()*1000)//v.STEP*v.STEP
    offset=boundary-base
    for key in ('formed_ms','expires_ms','as_of_ms'):c[key]+=offset
    for key in ('sweep_ms','swept_pivot_ms','break_ms','broken_pivot_ms','fvg_ms','poi_origin_ms'):
        if c['evidence'].get(key) is not None:c['evidence'][key]+=offset
    c['evidence']['fvg_origin_ms']=[x+offset for x in c['evidence']['fvg_origin_ms']]
    binding={k:val for k,val in c.items() if k not in ('candidate_id','as_of_ms','approved_at_ms')}
    c['candidate_id']=hashlib.sha256(json.dumps(binding,sort_keys=True).encode()).hexdigest()[:24]
    p=v.apply_review({'approve':True,'candidate_id':c['candidate_id']},[c],boundary)
    run=AiAnalysisRun(trigger='manual',status='completed',structured=p);session.add(run);await session.flush()
    monkeypatch.setenv('VERIFIED_PLANS_ENABLED','1')
    rows,meta=await persist_strategy_candidates(session,run_id=run.id,symbol='BTCUSDT',structured=p)
    assert rows,meta
    row=rows['ict_reversal'];row.lifecycle_state='READY_SHORT'
    settings=await get_or_create_settings(session)
    settings.initial_balance_set=True;settings.auto_open_paper_positions=True
    settings.default_margin_usdt=50;settings.default_leverage=10;settings.max_risk_pct_per_position=1
    settings.maker_fee_rate=.0002;settings.taker_fee_rate=.0005;settings.slippage_bps=1
    await append_ledger(session,entry_type=LEDGER_INITIAL,amount_usdt=1000)
    now_ms=boundary+120_000
    monkeypatch.setattr(auto,'_utc_now',lambda:datetime.fromtimestamp(now_ms/1000,timezone.utc))
    minutes=[Candle(live,live+.01,live-.01,live,now_ms-(120-i)*60000) for i in range(120)]
    bundle=KlineBundle(minutes,None,[],[],True,'1m')
    monkeypatch.setattr(MarketDataService,'fetch_kline_bundle',AsyncMock(return_value=bundle))
    request=dict(run_id=run.id,setup=row,validation={'acceptable_rr':True,'objective_ready':True},
        structured={**p,'decision':'SHORT'},symbol='BTCUSDT',commit=False,require_ai_flags=False,
        allow_pre_entry_without_touch=True,entry_mode='BALANCED',
        verified_5m=[replace(b,time_ms=b.time_ms+offset) for b in series['5m']],as_of_ms=now_ms)
    from app.services.opportunity_monitor import _attempt_ready_paper_order
    summary={'orders':0}
    result=await _attempt_ready_paper_order(session,row,
        now=datetime.fromtimestamp(now_ms/1000,timezone.utc),summary=summary,
        entry_mode='BALANCED',paper_entry_mode='BALANCED',market_data_verified=True,
        latest_closed_5m=request['verified_5m'][-1],closed_5m=request['verified_5m'])
    assert result,(row.last_validation_result,row.last_rejection_reason)
    assert summary['orders']==1 and row.lifecycle_state=='PENDING_ENTRY'
    pos=(await session.execute(select(SimPosition))).scalar_one()
    assert pos.status=='pending_entry' and pos.leverage==3
    assert pos.stop_loss==c['stop'] and pos.take_profit==c['target'] and pos.limit_entry_price==c['entry']
    assert pos.extra['verified_risk_budget']['risk_fraction']==.0025
    # A second submit is idempotently rejected rather than creating exposure.
    again=await maybe_auto_open_paper_position(session,**request)
    assert not again['created']


def test_source_audit_rejects_bad_ohlc():
    import importlib.util
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('replay',Path(__file__).parents[2]/'scripts/replay_verified_plans.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    result=module.replay([[0,101,100,98,99,1],[60000,99,100,98,99,1]])
    assert result['status']=='blocked_untrusted_source' and result['performance'] is None

@pytest.mark.asyncio
async def test_no_candidate_never_calls_paid_api(monkeypatch):
    from app.services import ai_ict_analysis as ai
    monkeypatch.setenv('VERIFIED_PLANS_ENABLED','1')
    monkeypatch.setattr(ai,'get_plain',AsyncMock(side_effect=['openai_api','high']))
    monkeypatch.setattr(ai,'get_secret',AsyncMock(return_value='synthetic-test-key'))
    monkeypatch.setattr(ai,'build_raw_market_payload',AsyncMock(return_value=({},1000,{})))
    call=AsyncMock();monkeypatch.setattr(ai,'stream_analysis_response',call)
    draft=await ai._execute_inference(run_id=123,trigger='scheduled',model_override=None)
    assert draft.status=='completed' and draft.validation['api_skipped'] is True
    assert draft.structured['setup_mode']=='flat'
    call.assert_not_called()


@pytest.mark.asyncio
async def test_daily_loss_breaker_and_account_exposure(session):
    from app.models import SimPosition
    from app.services.position.settings_service import get_or_create_settings
    from app.services.position.wallet_service import append_ledger,LEDGER_INITIAL
    from app.services.verified_plan_risk import budget
    await append_ledger(session,entry_type=LEDGER_INITIAL,amount_usdt=990)
    settings=await get_or_create_settings(session)
    setup=SimpleNamespace(direction='LONG',entry_price=1000,stop_loss=990)
    now=datetime.now(timezone.utc)
    # Minimum persisted fields: other numeric columns have database defaults.
    pos=SimPosition(public_id='risk-test',symbol='BTCUSDT',direction='LONG',status='closed',
        margin_usdt=50,leverage=3,order_type='limit',stop_loss=990,take_profit=1025,
        closed_at=now,net_pnl_usdt=-10)
    session.add(pos);await session.flush()
    assert (await budget(session,settings,setup,now))[1]=='verified_daily_loss_limit'
    pos.status='open';await session.flush()
    assert (await budget(session,settings,setup,now))[1]=='verified_one_position_limit'


def test_partial_five_minute_return_cannot_be_missed():
    c,s,cost,now,live=candidate()
    # A touch after the last 5m close, already visible in closed 1m history.
    minutes=[Candle(live,live+.01,live-.01,live,now+60_000-(120-i)*60_000) for i in range(120)]
    minutes[-1]=replace(minutes[-1],high=c['zone'][1]+.1)
    s['1m']=minutes
    plans,reasons=v.build_candidates(s,cost,now+60_000,live)
    assert not plans and reasons.get('already_retested')
