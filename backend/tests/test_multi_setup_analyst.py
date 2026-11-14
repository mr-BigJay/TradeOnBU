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
    evidence=[dict(id='zone',kind='resistance',timeframe='1h',time_ms=now-14400000,low=1020.,high=1022.,price=1021.,direction=None),
              dict(id='stop',kind='support',timeframe='1h',time_ms=now-18000000,low=1017.,high=1019.,price=1018.,direction=None),
              dict(id='support_zone',kind='support',timeframe='1h',time_ms=now-14400000,low=1020.,high=1022.,price=1021.,direction=None),
              dict(id='target',kind='resistance',timeframe='1h',time_ms=now-21600000,low=1060.,high=1062.,price=1061.,direction=None)]
    return series,costs,evidence,now


def proposal(sid):
    return dict(strategy_id=sid,direction='LONG',zone_id='zone' if sid=='breakout_retest' else 'support_zone',invalidation_id='stop',target_id='target',reason_fa='واکنش به سطح معتبر و فضای کافی تا مقاومت')


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
        rr=auto_paper_rr_from_settings(c,direction='LONG',entry=p['entry_price'],sl=p['stop_loss'],tp=p['take_profit'],min_net_rr=1.5)
        assert rr.acceptable
        assert p['take_profit']<1061


@pytest.mark.parametrize('key,value,reason',[
    ('zone_id','invented','unknown_evidence_id'),('strategy_id',[], 'invalid_strategy_direction'),
    ('target_id','stop','target_requires_structural_level'),('invalidation_id','support_zone','invalidation_inside_zone'),
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
    # Synthetic bars have no independently audited reaction event.
    from app.services import analyst_confirmation
    monkeypatch.setattr(analyst_confirmation, 'recent_confirmations', lambda *_: {})
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
    assert _effective_interval_minutes(cfg)==60
    nxt=_next_regular_run_at(cfg,datetime(2026,11,7,10,10,tzinfo=timezone.utc))
    assert nxt==datetime(2026,11,7,11,1,tzinfo=timezone.utc)

@pytest.mark.asyncio
@pytest.mark.parametrize('malformed',[False,True,'partial','quota'])
async def test_api_discovers_plans_without_old_candidate_and_persists_before_delivery(session,monkeypatch,malformed):
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
        'forecast_scenarios':[{'scenario_type':'resistance_rejection','direction':'SHORT',
            'level_id':'zone','target_id':'stop','reason_fa':'واکنش احتمالی به مقاومت ثبت‌شده'}],
        'market_summary_fa':'خلاصه','comparison_fa':'مقایسه'}),requested_model='test',requested_reasoning_effort='high',
        confirmed_reasoning_effort='high',response_model='test',response_id='test',response_metadata={'usage':{'input_tokens':100,'output_tokens':50}}))
    monkeypatch.setattr(ai,'stream_analysis_response',inference)
    if malformed:inference.return_value.text='invalid json response'
    if malformed in ('partial','quota'):
        from app.services.siwc.responses_client import OpenAIResponsesError
        inference.side_effect=OpenAIResponsesError('stream_interrupted' if malformed=='partial' else 'insufficient_quota',
            'synthetic provider error',partial=inference.return_value,http_status=429 if malformed=='quota' else None)
    monkeypatch.setattr(ai,'_generate_chart_for_run',AsyncMock(side_effect=ValueError('synthetic unavailable')))
    async def delivery(**kwargs):
        async with TestSession() as check:
            rows=(await check.execute(select(AiProposedSetup))).scalars().all()
            assert len(rows)==3
    monkeypatch.setattr(ai,'_deliver_ai_telegram',delivery)
    run=AiAnalysisRun(trigger='manual',status='running',structured={});session.add(run);await session.commit()
    draft=await ai._execute_inference(run_id=run.id,trigger='manual',model_override=None)
    if malformed:
        assert draft.status=='failed'
        assert draft.structured['_artifacts']['inference']['response_metadata']['usage']['output_tokens']==50
        assert draft.structured['_artifacts']['inference']['response_text']=='invalid json response'
        assert draft.structured['analyst_review'].get('retryable') is (malformed=='partial')
        return
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
    # Synthetic execution fixtures deliberately contain no independently proven
    # HTF zones. They must not leak into the major-level watchlist.
    assert payload['outlook_major_levels']==[]
    forecasts=draft.structured['analyst_summary']['forecast_scenarios']
    assert forecasts==[]
    assert '1:not_proven_major_htf_level' in draft.structured['analyst_summary']['forecast_rejections']
    assert 'چشم‌انداز مشروط چهار‌ساعته' in draft.report_fa
    assert 'سطوح اصلی:' in draft.report_fa
    assert draft.structured['analyst_review']['payload_sizes']['instructions_chars']>500
    assert draft.structured['analyst_review']['payload_sizes']['provider_usage']['input_tokens']==100
    second=await ai._execute_inference(run_id=run.id,trigger='manual',model_override=None)
    assert second.validation['reason']=='analyst_slot_already_attempted'
    inference.assert_awaited_once()

@pytest.mark.asyncio
@pytest.mark.parametrize('sid,deep_sweep',[(sid,False) for sid in a.PROFILES]+[('ict_reversal',True)])
async def test_real_monitor_confirms_then_places_limit_and_fills_later(session,monkeypatch,sid,deep_sweep):
    from app.domain import trading_calendar
    monkeypatch.setattr(trading_calendar, "new_entries_allowed", lambda _now: True)
    from app.models import AiAnalysisRun,SimPosition
    from app.services.strategy_candidates import persist_strategy_candidates
    from app.services.position.settings_service import get_or_create_settings
    from app.services.position.wallet_service import append_ledger,LEDGER_INITIAL
    from app.services.market_data import MarketDataService,KlineBundle
    from app.services import opportunity_monitor as monitor, paper_auto_trading as auto
    from app.services.position.position_monitor import SimPositionMonitorService
    from sqlalchemy import select
    monkeypatch.setenv('MULTI_SETUP_ANALYST_ENABLED','1')
    s,c,e,n=fixture()
    # The analyzed history and monitor history agree on the confirmed pivot.
    s['5m']=[Candle(1023,1024,1019.5,1023,b.time_ms) for b in s['5m']]
    p=a.compile_plan(proposal(sid),e,s,c,n,n)
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
        events=[Candle(1023,1024,1012 if deep_sweep else 1017,1021,boundary),Candle(1021,1026,1021,1025,boundary+300000)]
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
    first_positions=(await session.execute(select(SimPosition))).scalars().all()
    # A sweep/reclaim or C breakout can place a *resting* limit after one
    # closed candle; B's first red candle is not a valid bullish rejection.
    assert len(first_positions)==(0 if sid=='trend_continuation' else 1)
    assert all(p.status=='pending_entry' for p in first_positions)
    five.append(events[1]);now_ms=boundary+600000;refresh_minutes()
    await svc.monitor_setups(session)
    positions=(await session.execute(select(SimPosition))).scalars().all()
    assert len(positions)==1,(row.confirmation_state,row.last_rejection_reason,row.last_validation_result)
    pos=positions[0]
    assert pos.status=='pending_entry' and pos.leverage==3
    if deep_sweep:
        assert pos.stop_loss<min(b.low for b in events)<p['stop_loss']
        assert pos.take_profit>p['take_profit']
        assert pos.extra['execution_plan']['proposal_stop']==p['stop_loss']
        from app.services.execution_model_sim import auto_paper_rr_from_settings
        assert auto_paper_rr_from_settings(settings,direction='LONG',entry=pos.entry_price,
            sl=pos.stop_loss,tp=pos.take_profit,min_net_rr=1.5).acceptable
        quantity=pos.margin_usdt*pos.leverage/pos.entry_price
        adverse_exit=pos.stop_loss*(1-settings.slippage_bps/10000)
        stop_cost=quantity*(pos.entry_price-adverse_exit+pos.entry_price*settings.maker_fee_rate+adverse_exit*settings.taker_fee_rate)
        assert stop_cost<=pos.extra['verified_risk_budget']['equity']*.0025
    assert set(row.confirmation_state['confirmation_timestamps'].values())==({boundary+300000} if sid=='trend_continuation' else {boundary})
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
    from app.domain import trading_calendar
    monkeypatch.setattr(trading_calendar, "new_entries_allowed", lambda _now: True)
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


def test_execution_rejects_micro_pivot_as_primary_level():
    """Five-minute pivots are not standalone 1H/4H trading support/resistance."""
    series, costs, evidence, now = fixture()
    evidence[0]["timeframe"] = "5m"
    with pytest.raises(ValueError, match="execution_requires_major_structure"):
        a.compile_plan(proposal("breakout_retest"), evidence, series, costs, now, now)


def test_daily_interval_is_supported_for_outlook_generation():
    from app.domain.candle_utils import interval_ms
    assert interval_ms("1d") == 86_400_000
    assert interval_ms("4h") == 14_400_000


def test_preconfirmed_closed_candles_can_seed_paper_setup_without_nameerror(monkeypatch):
    """A proven closed 5m HTF reaction survives parsing without inventing a fill."""
    from app.services import analyst_confirmation
    from app.services.conditional_setup import (
        apply_conditional_plan_to_setup_row, parse_conditional_plan,
    )

    series, costs, evidence, now = fixture()
    boundary = now // 300_000 * 300_000
    stamps = {
        "htf_pullback_rejection": boundary - 300_000,
    }
    seed = {
        "confirmation_timestamps": stamps,
        "confirmation_evidence": {
            token: {"bar_low": 1019., "bar_high": 1024., "bar_ms": ms}
            for token, ms in stamps.items()
        },
    }
    monkeypatch.setattr(analyst_confirmation, "recent_confirmations", lambda *_args: seed)
    structured = a.compile_plan(proposal("trend_continuation"), evidence, series, costs, now, now)
    assert structured["verified_trade_plan"]["seed_confirmations"] == seed

    plan, error = parse_conditional_plan(structured)
    assert error is None and plan is not None
    row = SimpleNamespace()
    apply_conditional_plan_to_setup_row(
        row, plan=plan, run_id=1, structured=structured,
        created_at=datetime.fromtimestamp((boundary + 300_000) / 1000, timezone.utc),
    )
    assert row.confirmation_state["confirmation_timestamps"] == stamps
    assert row.confirmation_state["completed"] == ["htf_pullback_rejection"]
    assert row.confirmation_state["missing"] == ["entry_zone_touch"]
    assert row.lifecycle_state.startswith("WAIT")


@pytest.mark.parametrize("sid", list(a.PROFILES))
def test_fast_analyst_confirmation_never_accepts_unsigned_ai_plan(sid):
    from app.services.conditional_setup import parse_conditional_plan
    series,costs,evidence,now=fixture()
    signed=a.compile_plan(proposal(sid),evidence,series,costs,now,now)
    assert parse_conditional_plan(signed)[1] is None
    unsigned=dict(signed)
    unsigned.pop("verified_trade_plan",None)
    assert parse_conditional_plan(unsigned)[1]=="strategy_profile_mismatch"


def test_fast_triggers_are_closed_bar_price_reactions_not_ai_flags():
    from app.services.ict_confirmation_engine import _token_on_bar
    sweep={'verified_trade_plan':{'confirmation_reference':{'liquidity_price':1018}}}
    assert _token_on_bar("htf_sweep_reclaim", direction="LONG",
        window=[Candle(1023,1024,1017,1021,300000)],
        zone_lo=1020,zone_hi=1022,structured=sweep)
    assert not _token_on_bar("htf_sweep_reclaim",direction="LONG",
        window=[Candle(1023,1024,1017,1017.5,300000)],
        zone_lo=1020,zone_hi=1022,structured=sweep)
    assert _token_on_bar("htf_pullback_rejection",direction="LONG",
        window=[Candle(1021,1026,1020,1025,300000)],
        zone_lo=1020,zone_hi=1022,structured={})
    assert not _token_on_bar("htf_pullback_rejection",direction="LONG",
        window=[Candle(1023,1025,1020,1021,300000)],
        zone_lo=1020,zone_hi=1022,structured={})
    breakout={'breakout_level':1021}
    prior=Candle(1020,1021,1019,1020,0)
    assert _token_on_bar("breakout_close",direction="LONG",
        window=[prior,Candle(1020,1025,1020,1024,300000)],
        zone_lo=1020,zone_hi=1022,structured=breakout)
    assert not _token_on_bar("breakout_close",direction="LONG",
        window=[prior,Candle(1020,1025,1020,1020.5,300000)],
        zone_lo=1020,zone_hi=1022,structured=breakout)


def test_fast_analyst_requires_correct_htf_role_and_fully_confirmed_levels():
    s,c,e,n=fixture()
    wrong=deepcopy(e);wrong[2]['kind']='resistance'
    with pytest.raises(ValueError,match='reversal_continuation_requires_support_resistance'):
        a.compile_plan(proposal("trend_continuation"),wrong,s,c,n,n)
    wrong=deepcopy(e);wrong[1]['timeframe']='15m'
    with pytest.raises(ValueError,match='stop_requires_major_structure'):
        a.compile_plan(proposal("ict_reversal"),wrong,s,c,n,n)
    wrong=deepcopy(e);wrong[-1]['timeframe']='15m'
    with pytest.raises(ValueError,match='target_requires_structural_level'):
        a.compile_plan(proposal("ict_reversal"),wrong,s,c,n,n)
    wrong=deepcopy(e);wrong[0]['confirmed_ms']=n+300000
    with pytest.raises(ValueError,match='major_structure_not_yet_confirmed'):
        a.compile_plan(proposal("breakout_retest"),wrong,s,c,n,n)


@pytest.mark.asyncio
async def test_new_analyst_paper_position_exits_at_48h_only_after_closed_bar():
    from app.services.position.position_monitor import SimPositionMonitorService
    from app.services.multi_setup_analyst import VERSION
    entry_ms=300_000
    details={"verified_trade_plan":{"version":VERSION,"confirmation_type":"htf_pullback_rejection"},
             "entry_fill_bar_ms":entry_ms}
    pos=SimpleNamespace(direction="LONG",fill_price=1000.,entry_price=1000.,
        leverage=3,stop_loss=900.,take_profit=1100.,extra=details)
    svc=SimPositionMonitorService()
    svc.positions._close_position=AsyncMock()
    early=Candle(1000,1001,999,1000,entry_ms+24*60*60*1000)
    assert not await svc._handle_open_bar(None,pos,None,early)
    svc.positions._close_position.assert_not_awaited()
    final=Candle(1000,1001,999,1000,entry_ms+48*60*60*1000-60_000)
    assert await svc._handle_open_bar(None,pos,None,final)
    assert svc.positions._close_position.await_args.kwargs["reason"]=="time_exit"
    svc.positions._close_position.reset_mock()
    stop_first=Candle(950,1001,890,895,final.time_ms)
    assert await svc._handle_open_bar(None,pos,None,stop_first)
    assert svc.positions._close_position.await_args.kwargs["reason"]=="sl_hit"
    svc.positions._close_position.reset_mock()
    pos.extra={"verified_trade_plan":{"version":"legacy"},"entry_fill_bar_ms":entry_ms}
    assert not await svc._handle_open_bar(None,pos,None,final)
    svc.positions._close_position.assert_not_awaited()


def test_weekend_policy_is_tehran_local_and_blocks_only_new_entries():
    from app.domain.trading_calendar import new_entries_allowed
    assert new_entries_allowed(datetime(2026,11,13,20,29,tzinfo=timezone.utc))
    assert not new_entries_allowed(datetime(2026,11,13,20,30,tzinfo=timezone.utc))
    assert not new_entries_allowed(datetime(2026,11,15,20,29,tzinfo=timezone.utc))
    assert new_entries_allowed(datetime(2026,11,15,20,30,tzinfo=timezone.utc))
