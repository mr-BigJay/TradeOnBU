"""Synthetic contract, persistence and execution checks; not profitability evidence."""
import json
import math
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.domain.candles import Candle
from app.services import multi_setup_analyst as a, verified_trade_plan as v


def fixture():
    now=int(datetime.now(timezone.utc).timestamp()*1000)
    series={}
    for tf,step in a.STEPS.items():
        boundary=now//step*step
        # Rising swing highs AND lows, not merely a moving-average approximation.
        prices=[1000+i*.7+5*math.sin(i*math.pi/4) for i in range(48)]
        series[tf]=[Candle(p,p+2,p-2,p,boundary-(48-i)*step) for i,p in enumerate(prices)]
    costs=SimpleNamespace(default_margin_usdt=50,default_leverage=3,maker_fee_rate=.0002,taker_fee_rate=.0005,slippage_bps=1,default_tp_behavior='market')
    evidence=[dict(id='zone',kind='resistance',timeframe='5m',time_ms=now-300000,low=1020.,high=1022.,price=1021.,direction=None),
              dict(id='stop',kind='support',timeframe='15m',time_ms=now-900000,low=1017.,high=1019.,price=1018.,direction=None),
              dict(id='target',kind='resistance',timeframe='1h',time_ms=now-3600000,low=1060.,high=1062.,price=1061.,direction=None)]
    return series,costs,evidence,now


def proposal(sid):
    return dict(strategy_id=sid,direction='LONG',zone_id='zone',invalidation_id='stop',target_id='target',reason_fa='واکنش به سطح معتبر و فضای کافی تا مقاومت')


def response():
    s,c,e,n=fixture()
    return a.compile_response({'market_summary_fa':'بازار صعودی','comparison_fa':'مقایسه سه سناریو',
        'plans':[proposal(sid) for sid in a.PROFILES]},e,s,c,n,n,{sid:True for sid in a.PROFILES})


def test_three_independent_conditional_contracts_and_cost_floor():
    result=response()
    assert result['analyst_summary']['accepted']==3,result
    from app.services.execution_model_sim import auto_paper_rr_from_settings
    from app.services.entry_strategies import validate_strategy_plan
    s,c,e,n=fixture()
    for p in [result,*result['strategy_candidates'].values()]:
        assert v.plan_error(p,n) is None
        assert validate_strategy_plan(p) is None
        assert p['decision']=='NO_TRADE' and p['final_ai_decision']=='WAIT'
        assert not p['structural_confirmation_clear']
        assert p['stop_loss']<p['structural_invalidation_level']<p['entry_zone_low']
        rr=auto_paper_rr_from_settings(c,direction='LONG',entry=p['entry_price'],sl=p['stop_loss'],tp=p['take_profit'],min_net_rr=1.4)
        assert rr.acceptable
        assert p['take_profit']<1061


@pytest.mark.parametrize('key,value,reason',[
    ('zone_id','invented','unknown_evidence_id'),('strategy_id',[], 'invalid_strategy_direction'),
    ('target_id','stop','target_requires_structural_level'),('invalidation_id','zone','invalidation_inside_zone'),
    ('direction','UP','invalid_strategy_direction')])
def test_bad_ai_proposal_is_rejected_without_losing_siblings(key,value,reason):
    s,c,e,n=fixture();bad=proposal('ict_reversal');bad[key]=value
    r=a.compile_response({'plans':[bad,proposal('breakout_retest')]},e,s,c,n,n,{sid:True for sid in a.PROFILES})
    assert reason in r['analyst_summary']['rejections']
    assert r['analyst_summary']['accepted']==1


def test_contract_tampering_expiry_and_insufficient_reward():
    p=response()
    for field in ('entry_price','stop_loss','take_profit'):
        bad=deepcopy(p);bad[field]+=1
        assert v.plan_error(bad)=='verified_plan_levels_changed'
    bad=deepcopy(p);bad['confirmation_type']='level_breakout_retest'
    assert v.plan_error(bad)=='verified_plan_contract_changed'
    assert v.plan_error(p,p['verified_trade_plan']['expires_ms'])=='verified_plan_expired'
    s,c,e,n=fixture();e[-1]['price']=1023
    with pytest.raises(ValueError,match='insufficient_net_reward'):
        a.compile_plan(proposal('ict_reversal'),e,s,c,n,n)


def test_catalog_rejects_missing_or_unclosed_data():
    s,c,e,n=fixture()
    assert a.catalog(s,n)
    s['1m'].pop()
    with pytest.raises(ValueError,match='invalid_or_stale_1m'):a.catalog(s,n)


def test_continuation_requires_structural_trend():
    s,c,e,n=fixture()
    s['1h']=[Candle(1000,1002,998,1000,b.time_ms) for b in s['1h']]
    with pytest.raises(ValueError,match='continuation_trend_conflict'):
        a.compile_plan(proposal('trend_continuation'),e,s,c,n,n)


@pytest.mark.asyncio
async def test_persists_three_setups_without_fabricating_confirmations(session,monkeypatch):
    from app.models import AiAnalysisRun
    from app.services.strategy_candidates import persist_strategy_candidates
    from app.services.conditional_revision import objective_confirmations_satisfied_for_order
    monkeypatch.setenv('MULTI_SETUP_ANALYST_ENABLED','1')
    p=response();run=AiAnalysisRun(trigger='manual',status='completed',structured=p)
    session.add(run);await session.flush()
    rows,meta=await persist_strategy_candidates(session,run_id=run.id,symbol='BTCUSDT',structured=p)
    assert set(rows)==set(a.PROFILES),meta
    for row in rows.values():
        assert not (row.confirmation_state or {}).get('confirmation_timestamps')
        assert not objective_confirmations_satisfied_for_order(row,allow_pre_entry_without_touch=True)[0]


@pytest.mark.asyncio
async def test_paid_slot_is_consumed_even_when_request_fails(session):
    from app.models import AiAnalysisRun
    now=datetime.now(timezone.utc)
    allowed,slot=await a.review_allowed(session,now);assert allowed
    session.add(AiAnalysisRun(trigger='manual',status='failed',created_at=now,structured={'analyst_review':{'slot_ms':slot,'api_attempted':True}}))
    await session.flush()
    assert await a.review_allowed(session,now)==(False,slot)
    assert (await a.review_allowed(session,now+timedelta(hours=4)))[0]


def test_four_hour_schedule_overrides_legacy_five_minute_setting(monkeypatch):
    from app.models import AiAnalysisSettings
    from app.services.ai_analysis_scheduler import _effective_interval_minutes,_next_regular_run_at
    monkeypatch.setenv('MULTI_SETUP_ANALYST_ENABLED','1')
    cfg=AiAnalysisSettings(interval_minutes=5)
    assert _effective_interval_minutes(cfg)==240
    nxt=_next_regular_run_at(cfg,datetime(2026,11,7,10,10,tzinfo=timezone.utc))
    assert nxt==datetime(2026,11,7,12,1,tzinfo=timezone.utc)

@pytest.mark.asyncio
async def test_api_discovers_plans_without_old_candidate_and_persists_before_delivery(session,monkeypatch):
    from app.models import AiAnalysisRun, AiProposedSetup
    from sqlalchemy import select
    from app.services import ai_ict_analysis as ai
    from app.config import get_settings
    from tests.conftest import TestSession
    monkeypatch.setenv('MULTI_SETUP_ANALYST_ENABLED','1')
    monkeypatch.setenv('AI_PROVIDER','openai_api');monkeypatch.setenv('OPENAI_API_KEY','sk-test-only');get_settings.cache_clear()
    monkeypatch.setattr(ai,'SessionLocal',TestSession)
    s,c,e,n=fixture()
    monkeypatch.setattr(ai,'build_raw_market_payload',AsyncMock(return_value=({'symbol':'BTCUSDT'},s['1m'][-1].close,s)))
    monkeypatch.setattr(ai,'build_ai_memory_context',AsyncMock(return_value={}))
    monkeypatch.setattr(ai,'load_ai_market_intelligence',AsyncMock(return_value={'funding':{'available':False}}))
    # Catalog geometry is separately tested; this fixture isolates inference and persistence wiring.
    monkeypatch.setattr(a,'catalog',lambda *args:e)
    legacy=AsyncMock(side_effect=AssertionError('old reversal detector must not gate AI'))
    monkeypatch.setattr(v,'build_candidates',legacy)
    inference=AsyncMock(return_value=SimpleNamespace(text=json.dumps({'plans':[proposal(sid) for sid in a.PROFILES],
        'market_summary_fa':'خلاصه','comparison_fa':'مقایسه'}),requested_model='test',requested_reasoning_effort='high',
        confirmed_reasoning_effort='high',response_model='test',response_id='test',response_metadata={'usage':{'input_tokens':100,'output_tokens':50}}))
    monkeypatch.setattr(ai,'stream_analysis_response',inference)
    monkeypatch.setattr(ai,'_generate_chart_for_run',AsyncMock(side_effect=ValueError('synthetic unavailable')))
    async def delivery(**kwargs):
        async with TestSession() as check:
            rows=(await check.execute(select(AiProposedSetup))).scalars().all()
            assert len(rows)==3
    monkeypatch.setattr(ai,'_deliver_ai_telegram',delivery)
    run=AiAnalysisRun(trigger='manual',status='running',structured={});session.add(run);await session.commit()
    draft=await ai._execute_inference(run_id=run.id,trigger='manual',model_override=None)
    assert draft.status.startswith('completed'),(draft.error_code,draft.error_detail)
    assert draft.persisted and draft.structured['analyst_summary']['accepted']==3
    assert 'delivery_error' not in draft.structured.get('_artifacts',{}).get('telegram',{})
    inference.assert_awaited_once();legacy.assert_not_called()
    payload=json.loads(inference.call_args.kwargs['user_input'])
    assert payload['evidence_catalog']==e and payload['market_intelligence']['funding']['available'] is False
    assert set(payload['enabled_strategies'])==set(a.PROFILES)
    assert set(payload['strategy_features']['timeframes'])=={'5m','15m','1h','4h'}
    assert all('rsi14' in f and 'ema9' in f for f in payload['strategy_features']['timeframes'].values())
    assert draft.structured['_artifacts']['strategy_features']==payload['strategy_features']
    second=await ai._execute_inference(run_id=run.id,trigger='manual',model_override=None)
    assert second.validation['reason']=='analyst_slot_already_attempted'
    inference.assert_awaited_once()

@pytest.mark.asyncio
@pytest.mark.parametrize('sid',list(a.PROFILES))
async def test_real_monitor_confirms_then_places_limit_and_fills_later(session,monkeypatch,sid):
    from app.models import AiAnalysisRun,SimPosition
    from app.services.strategy_candidates import persist_strategy_candidates
    from app.services.position.settings_service import get_or_create_settings
    from app.services.position.wallet_service import append_ledger,LEDGER_INITIAL
    from app.services.market_data import MarketDataService,KlineBundle
    from app.services import opportunity_monitor as monitor, paper_auto_trading as auto
    from app.services.position.position_monitor import SimPositionMonitorService
    from sqlalchemy import select
    monkeypatch.setenv('MULTI_SETUP_ANALYST_ENABLED','1')
    s,c,e,n=fixture();p=a.compile_plan(proposal(sid),e,s,c,n,n)
    run=AiAnalysisRun(trigger='manual',status='completed',structured=p);session.add(run);await session.flush()
    rows,meta=await persist_strategy_candidates(session,run_id=run.id,symbol='BTCUSDT',structured=p)
    assert sid in rows,meta
    row=rows[sid]
    settings=await get_or_create_settings(session)
    settings.initial_balance_set=True;settings.auto_open_paper_positions=True
    settings.default_margin_usdt=50;settings.default_leverage=10;settings.max_risk_pct_per_position=1
    settings.maker_fee_rate=.0002;settings.taker_fee_rate=.0005;settings.slippage_bps=1
    await append_ledger(session,entry_type=LEDGER_INITIAL,amount_usdt=1000)
    boundary=(n//300000+1)*300000
    previous=1020 if sid=='breakout_retest' else 1023
    five=[Candle(previous,1024,1019.5,previous,boundary-(60-i)*300000) for i in range(60)]
    if sid=='ict_reversal':
        events=[Candle(1023,1024,1018,1021,boundary),Candle(1021,1026,1021,1025,boundary+300000)]
    elif sid=='trend_continuation':
        events=[Candle(1023,1024,1020,1021,boundary),Candle(1021,1026,1021,1025,boundary+300000)]
    else:
        events=[Candle(1020,1025,1020,1024,boundary),Candle(1024,1025,1020.5,1024,boundary+300000)]
    now_ms=boundary+300000
    minutes=[]
    def refresh_minutes():
        minutes[:]=[Candle(1025,1025.2,1024.8,1025,now_ms-(120-i)*60000) for i in range(120)]
    refresh_minutes();five.append(events[0])
    async def bundle_for(self,symbol,interval,limit):
        return KlineBundle(minutes if interval=='1m' else five,None,[],[],True,interval)
    monkeypatch.setattr(MarketDataService,'fetch_kline_bundle',bundle_for)
    monkeypatch.setattr(monitor,'_utc_now',lambda:datetime.fromtimestamp(now_ms/1000,timezone.utc))
    monkeypatch.setattr(auto,'_utc_now',lambda:datetime.fromtimestamp(now_ms/1000,timezone.utc))
    svc=monitor.OpportunityMonitorService(MarketDataService())
    await svc.monitor_setups(session)
    assert not (await session.execute(select(SimPosition))).scalars().all()
    # Second distinct closed 5m bar completes structural conditions, not a 1m surrogate.
    five.append(events[1]);now_ms=boundary+600000;refresh_minutes()
    await svc.monitor_setups(session)
    positions=(await session.execute(select(SimPosition))).scalars().all()
    assert len(positions)==1,(row.confirmation_state,row.last_rejection_reason,row.last_validation_result)
    pos=positions[0]
    assert pos.status=='pending_entry' and pos.leverage==3
    assert set(row.confirmation_state['confirmation_timestamps'].values())=={boundary,boundary+300000}
    assert pos.extra['verified_risk_budget']['risk_fraction']==.0025
    touch_ms=max(now_ms,(pos.monitor_from_ms//60000+1)*60000)
    now_ms=touch_ms+60000;refresh_minutes()
    index=next(i for i,b in enumerate(minutes) if b.time_ms==touch_ms)
    minutes[index]=Candle(1025,1025.2,p['entry_price']-.1,p['entry_price'],touch_ms)
    await SimPositionMonitorService(MarketDataService()).monitor_session(session)
    assert pos.status=='open',(pos.extra,row.last_rejection_reason)
    assert pos.fill_price==p['entry_price']

@pytest.mark.asyncio
async def test_daily_outlook_remains_daily_without_duplicate_four_hour_calls(monkeypatch):
    from app.services import daily_outlook
    monkeypatch.setenv('MULTI_SETUP_ANALYST_ENABLED','1')
    monkeypatch.setenv('OUTLOOK_FOUR_HOUR_ENABLED','1')
    reserve=AsyncMock(return_value=(None,None,'not_due'))
    monkeypatch.setattr(daily_outlook,'reserve_run',reserve)
    await daily_outlook.daily_outlook_tick()
    reserve.assert_awaited_once_with('scheduled','daily')

@pytest.mark.asyncio
async def test_new_strategy_switches_do_not_destroy_legacy_preferences(session,monkeypatch):
    from app.services.entry_strategies import strategy_settings,save_strategy_settings
    monkeypatch.setenv('MULTI_SETUP_ANALYST_ENABLED','0')
    legacy={sid:sid=='ict_reversal' for sid in a.PROFILES}
    await save_strategy_settings(session,legacy)
    monkeypatch.setenv('MULTI_SETUP_ANALYST_ENABLED','1')
    assert all((await strategy_settings(session)).values())
    await save_strategy_settings(session,{sid:sid!='ict_reversal' for sid in a.PROFILES})
    monkeypatch.setenv('MULTI_SETUP_ANALYST_ENABLED','0')
    assert await strategy_settings(session)==legacy
