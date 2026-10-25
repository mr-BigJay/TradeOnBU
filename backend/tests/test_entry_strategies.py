import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.domain.candles import Candle
from app.services.entry_strategies import A, B, C, LABELS, PROFILES, SEQUENCES, strategy_settings, save_strategy_settings, shared_strategy_instructions, validate_strategy_plan
from app.services.ai_trader_instructions import ICT_TRADER_INSTRUCTIONS
from app.services.conditional_setup import parse_conditional_plan
from app.services.confirmation_policy import resolve_confirmation_policy, order_entry_allowed
from app.services.ict_confirmation_engine import process_bar_sequential, _token_on_bar


def plan(sid=B, direction='LONG'):
    long = direction == 'LONG'
    return dict(strategy_id=sid, decision='NO_TRADE', final_ai_decision='WAIT', setup_mode='conditional',
        potential_direction=direction, conditional_entry_zone_low=99, conditional_entry_zone_high=101,
        confirmation_type=PROFILES[sid], structural_invalidation_level=94 if long else 106,
        stop_loss=94 if long else 106, take_profit=125 if long else 75, breakout_level=100,
        market_regime='bullish' if long else 'bearish', market_regime_confidence='low',
        trade_against_dominant_trend=False, proposed_setup={'new_setup': True},
        trade_reason='پلن مشروط برای پایش', confirmation_required='تأیید کندل بسته‌شده لازم است',
        conditional_invalidation='بسته شدن زیر سطح ابطال ساختاری', liquidity_event_clear=False)


def bar(t,o,h,l,c):
    return Candle(time_ms=t,open=o,high=h,low=l,close=c,volume=1)


@pytest.mark.parametrize('sid',[B,C])
@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_new_plans_have_own_mandatory_profile(sid,direction):
    parsed,err=parse_conditional_plan(plan(sid,direction))
    assert err is None and parsed['strategy_id']==sid
    seq=parsed['confirmation_sequence']
    assert 'liquidity_sweep' not in seq
    policy=resolve_confirmation_policy(sequence=seq,structured={**plan(sid,direction),'optional_confirmations':seq})
    assert list(policy.required)==seq
    assert not order_entry_allowed(policy,{},allow_pre_entry_without_touch=True,zone_lo=99,zone_hi=101)[0]


@pytest.mark.parametrize('change,error',[
    ({'strategy_id':'invented'},'unknown_entry_strategy'),
    ({'setup_mode':'immediate','decision':'LONG'},'new_strategy_requires_objective_monitor'),
    ({'confirmation_type':'sweep_mss_poi'},'strategy_profile_mismatch'),
    ({'market_regime':'bearish'},'continuation_requires_aligned_trend'),
    ({'take_profit':110},'strategy_rr_below_two'),
    ({'stop_loss':float('inf')},'invalid_strategy_levels'),
    ({'stop_loss':102},'invalid_strategy_levels'),
])
def test_invalid_new_plans_fail_closed(change,error):
    assert validate_strategy_plan({**plan(),**change})==error


def test_breakout_requires_fixed_level_inside_zone():
    assert validate_strategy_plan({**plan(C),'breakout_level':120})=='breakout_level_outside_entry_zone'
    assert validate_strategy_plan({**plan(C),'breakout_level':None})=='invalid_strategy_levels'
    assert validate_strategy_plan({**plan(C),'strategy_id':A})=='strategy_profile_mismatch'


@pytest.mark.parametrize('sid',[B,C])
@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_closed_bar_sequence_no_sweep_and_no_early_ready(sid,direction):
    data=plan(sid,direction)
    candles=[bar(0,99,99.8,98,99),bar(300000,99,103,98,102),bar(600000,102,103,99.5,102),bar(900000,102,103,100,102)]
    if sid==B:
        candles=[bar(0,103,104,102,103),bar(300000,102,102,99,100),bar(600000,100,104,100,103),bar(900000,103,103,100,102)]
    if direction=='SHORT':
        candles=[bar(c.time_ms,200-c.open,200-c.low,200-c.high,200-c.close) for c in candles]
    sequence=SEQUENCES[PROFILES[sid]];state={}
    for i,candle in enumerate(candles[1:],1):
        progress=process_bar_sequential(direction=direction,sequence=sequence,confirmation_state=state,bar=candle,
            window=candles[:i+1],zone_lo=99,zone_hi=101,invalidation_level=data['structural_invalidation_level'],structured=data,monitor_from_ms=300000)
        state={'confirmation_timestamps':progress.confirmation_timestamps,'confirmation_evidence':progress.confirmation_evidence}
        assert len(progress.completed)==i
    assert not progress.missing


def test_breakout_wick_and_failed_retest_rejected():
    for token,window in [('breakout_close',[bar(0,99,99,98,99),bar(1,99,102,98,99)]),('breakout_retest',[bar(1,102,103,99,99)])]:
        assert not _token_on_bar(token,direction='LONG',window=window,zone_lo=99,zone_hi=101,structured=plan(C))


def test_balanced_does_not_reuse_breakout_wick_for_retest():
    from app.services.balanced_confirmation_engine import _try_token_on_1m
    one=[bar(0,99,102,99,101),bar(60000,102,104,101.5,103)]
    assert not _try_token_on_1m(token='breakout_retest',direction='LONG',bar_1m=one[-1],bar_5m=one[0],closed_5m=[],closed_1m=one,zone_lo=99,zone_hi=101,structured=plan(C))


def test_changing_breakout_level_resets_confirmation():
    from app.services.conditional_revision import assess_conditional_revision_impact,RevisionImpact
    row=SimpleNamespace(conditional_plan={'strategy_id':C,'breakout_level':100})
    assert assess_conditional_revision_impact(row,{'strategy_id':C,'breakout_level':100.1})==RevisionImpact.STRUCTURAL


@pytest.mark.asyncio
async def test_switches_block_inflight_candidate(session):
    from app.services.ai_setup_memory import create_setup_from_structured
    assert all((await strategy_settings(session)).values())
    await save_strategy_settings(session,{A:True,B:False,C:True});await session.commit()
    row,meta=await create_setup_from_structured(session,run_id=1,symbol='BTCUSDT',structured=plan(B))
    assert row is None and meta['reason']=='entry_strategy_disabled'


@pytest.mark.asyncio
@pytest.mark.parametrize('sid',[B,C])
async def test_new_candidate_persists_as_wait(session,sid):
    from app.services.ai_setup_memory import create_setup_from_structured
    row,meta=await create_setup_from_structured(session,run_id=1,symbol='BTCUSDT',structured=plan(sid))
    assert meta['created'] and row.lifecycle_state=='WAIT_LONG'
    assert row.structured_snapshot['strategy_id']==sid
    assert row.confirmation_state['required']==SEQUENCES[PROFILES[sid]]


@pytest.mark.asyncio
async def test_admin_only_settings_and_validation(client,admin_client):
    values={A:True,B:False,C:False}
    assert (await client.get('/api/gui/chatgpt/strategies')).status_code==200
    assert (await client.put('/api/gui/chatgpt/strategies/settings',json=values)).status_code==401
    assert (await admin_client.put('/api/gui/chatgpt/strategies/settings',json=values)).json()==values
    assert (await admin_client.put('/api/gui/chatgpt/strategies/settings',json={k:False for k in LABELS})).status_code==422
    assert (await client.get('/api/gui/chatgpt/strategies')).json()==values


@pytest.mark.asyncio
async def test_disabled_strategy_blocks_order_and_pending_fill(session):
    from app.models import AiProposedSetup,SimPosition
    from app.services.position.position_service import SimPositionService,PositionServiceError
    from app.services.position.execution_adapters import OrderRequest
    setup=AiProposedSetup(setup_id='strategy-test',run_id=1,symbol='BTCUSDT',direction='LONG',structured_snapshot=plan(B))
    session.add(setup);await save_strategy_settings(session,{A:True,B:False,C:True});await session.flush()
    svc=SimPositionService()
    req=OrderRequest(symbol='BTCUSDT',direction='LONG',margin_usdt=50,leverage=10,order_type='limit',entry_price=100,stop_loss=94,take_profit=125,ai_setup_id=setup.setup_id)
    with pytest.raises(PositionServiceError,match='entry_strategy_disabled'):
        await svc.create_order(session,req)
    pos=SimPosition(public_id='pending',symbol='BTCUSDT',direction='LONG',status='pending_entry',order_type='limit',margin_usdt=50,leverage=10,stop_loss=94,take_profit=125,ai_setup_id=setup.setup_id,extra={})
    session.add(pos);await session.flush()
    assert not await svc._fill_entry(session,pos,None,100,'test')
    assert pos.status=='cancelled'
    pos.status='open'
    assert await svc._fill_entry(session,pos,None,100,'test')
    assert pos.status=='open'


@pytest.mark.asyncio
async def test_single_request_keeps_usage_and_switch_snapshot(session,monkeypatch):
    from app.services import ai_ict_analysis as ai
    from app.config import get_settings
    from tests.conftest import TestSession
    monkeypatch.setenv('AI_PROVIDER','openai_api');monkeypatch.setenv('OPENAI_API_KEY','sk-test-only');get_settings.cache_clear()
    monkeypatch.setattr(ai,'SessionLocal',TestSession)
    monkeypatch.setattr(ai,'build_raw_market_payload',AsyncMock(return_value=({'symbol':'BTCUSDT'},100,{})))
    monkeypatch.setattr(ai,'build_ai_memory_context',AsyncMock(return_value={}))
    monkeypatch.setattr(ai,'load_ai_market_intelligence',AsyncMock(return_value={}))
    inference=AsyncMock(return_value=SimpleNamespace(text=json.dumps(plan(C)),requested_model='test',requested_reasoning_effort='high',confirmed_reasoning_effort='high',response_model='test',response_id='test',response_metadata={'usage':{'input_tokens':100,'output_tokens':50}}))
    monkeypatch.setattr(ai,'stream_analysis_response',inference)
    monkeypatch.setattr(ai,'_generate_chart_for_run',AsyncMock(side_effect=ValueError('test chart unavailable')))
    monkeypatch.setattr(ai,'_deliver_ai_telegram',AsyncMock())
    draft=await ai._execute_inference(run_id=1,trigger='test',model_override=None)
    assert draft.status.startswith('completed')
    inference.assert_awaited_once()
    assert B in inference.call_args.kwargs['instructions'] and C in inference.call_args.kwargs['instructions']
    audit=draft.structured['_artifacts']['inference']
    assert audit['response_metadata']['usage']['output_tokens']==50
    assert audit['enabled_strategies']=={k:True for k in LABELS}
    assert shared_strategy_instructions(ICT_TRADER_INSTRUCTIONS,{A:True,B:False,C:False})==ICT_TRADER_INSTRUCTIONS


@pytest.mark.asyncio
@pytest.mark.parametrize('sid',[B,C])
async def test_confirmed_new_strategy_can_create_paper_order_without_reversal_flags(session,sid):
    from app.services.ai_setup_memory import create_setup_from_structured
    from app.services.paper_auto_trading import maybe_auto_open_paper_position
    from tests.test_paper_auto_trading import _wallet_and_settings
    from app.models import SimPosition
    from sqlalchemy import select
    await _wallet_and_settings(session)
    data=plan(sid)
    row,meta=await create_setup_from_structured(session,run_id=1,symbol='BTCUSDT',structured=data)
    assert row is not None
    row.lifecycle_state='READY_LONG'
    seq=SEQUENCES[PROFILES[sid]]
    state=dict(row.confirmation_state)
    state.update(completed=seq,missing=[],confirmation_timestamps={token:300000*(i+1) for i,token in enumerate(seq)},
        confirmation_evidence={'entry_zone_touch':{'bar_low':99.5,'bar_high':101.5,'zone_lo':99,'zone_hi':101}})
    row.confirmation_state=state
    result=await maybe_auto_open_paper_position(session,run_id=1,setup=row,
        validation={'ai_decision':'LONG','acceptable_rr':True,'objective_ready':True},
        structured={**data,'decision':'LONG'},symbol='BTCUSDT',require_ai_flags=False)
    assert result['created'], result
    pos=(await session.execute(select(SimPosition).where(SimPosition.ai_setup_id==row.setup_id))).scalar_one()
    assert pos.extra['strategy_id']==sid
    assert pos.status=='pending_entry'


@pytest.mark.parametrize('sid',[B,C])
def test_balanced_new_tokens_work_with_actual_closed_one_minute_bars(sid):
    from app.services.balanced_confirmation_engine import _try_token_on_1m
    data=plan(sid)
    seq=SEQUENCES[PROFILES[sid]]
    one=([bar(0,103,104,102,103),bar(60000,102,102,99,100),bar(120000,100,104,100,103)] if sid==B else
         [bar(0,99,99.8,98,99),bar(60000,99,103,98,102),bar(120000,102,103,99.5,102)])
    for i,token in enumerate(seq[:2],1):
        assert _try_token_on_1m(token=token,direction='LONG',bar_1m=one[i],bar_5m=one[0],closed_5m=[],
            closed_1m=one[:i+1],zone_lo=99,zone_hi=101,structured=data)
