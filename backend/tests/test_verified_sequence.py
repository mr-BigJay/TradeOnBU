"""Sequence regressions; synthetic candles are not performance evidence."""
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import pytest
from app.domain.candles import Candle
from app.services import verified_trade_plan as v
from tests.test_verified_trade_plan import candidate


def test_delayed_reclaim_produces_fresh_plan_with_actual_confirmation_time():
    c,s,cost,now,live = candidate()
    i = next(i for i,b in enumerate(s['5m']) if b.time_ms==c['evidence']['sweep_ms'])
    ref = c['evidence']['swept_price']
    s['5m'][i] = replace(s['5m'][i], close=ref+.005)
    s['5m'][i+1] = replace(s['5m'][i+1], open=ref+.005, high=ref+.006)
    plans,reasons = v.build_candidates(s,cost,now,live)
    assert plans,reasons
    ev = plans[0]['evidence']
    assert ev['sweep_ms'] < ev['reclaim_ms'] <= ev['break_ms']
    assert ev['reclaim_ms']==s['5m'][i+1].time_ms
    # A move which never reclaims within three bars is still rejected.
    for j in range(i,i+3):
        s['5m'][j]=replace(s['5m'][j],close=ref+.005,high=max(ref+.01,s['5m'][j].high))
    plans,reasons=v.build_candidates(s,cost,now,live)
    assert not any(x['evidence']['sweep_ms']==s['5m'][i].time_ms for x in plans)
    assert reasons['sweep_not_reclaimed']


def test_zone_edge_does_not_consume_untouched_limit_price():
    c,s,cost,now,live=candidate()
    # SHORT: the lower zone edge was touched, but not the resting sell limit.
    edge=(c['zone'][0]+c['entry'])/2
    s['5m'][-1]=replace(s['5m'][-1],high=edge)
    plans,reasons=v.build_candidates(s,cost,now,live)
    assert any(x['entry']==c['entry'] for x in plans),reasons
    s['5m'][-1]=replace(s['5m'][-1],high=c['entry'])
    plans,reasons=v.build_candidates(s,cost,now,live)
    assert not any(x['opportunity_id']==c['opportunity_id'] for x in plans)
    assert reasons['already_retested']


@pytest.mark.parametrize('sign',[1,-1])
def test_same_bar_requires_ordered_complete_minute_evidence(sign):
    rows=[Candle(100,101,99,100,i*60000) for i in range(5)]
    rows[1]=Candle(100,101,97,100,60000)
    rows[3]=Candle(100,104,99,103,180000)
    rows[4]=Candle(103,104,102,103,240000)
    bar=Candle(100,104,97,103,0)
    ref,pivot=98,102
    if sign==-1:
        mirror=lambda x: Candle(200-x.open,200-x.low,200-x.high,200-x.close,x.time_ms)
        rows=list(map(mirror,rows));bar=mirror(bar);ref,pivot=200-ref,200-pivot
    assert v.same_bar_sequence(rows,bar,ref,pivot,sign)
    assert not v.same_bar_sequence(rows[:-1],bar,ref,pivot,sign)
    reversed_rows=[replace(rows[3],time_ms=0),*rows[1:]]
    assert not v.same_bar_sequence(reversed_rows,bar,ref,pivot,sign)
    # Both events in one minute still cannot be ordered from OHLC.
    rows[1]=replace(rows[3],time_ms=60000,low=bar.low,high=bar.high)
    assert not v.same_bar_sequence(rows,bar,ref,pivot,sign)


def test_countertrend_needs_first_new_displaced_15m_break():
    bars=[Candle(100,101,99,100,i*900000) for i in range(35)]
    bars[25]=Candle(100,105,99,100,25*900000)
    bars[30]=Candle(100,107,99,106,30*900000)
    proof=v.countertrend_break(bars,29*900000,1)
    assert proof and proof['time_ms']==bars[30].time_ms
    assert v.countertrend_break(bars,31*900000,1) is None
    bars[30]=replace(bars[30],open=105.5)
    assert v.countertrend_break(bars,29*900000,1) is None


def test_countertrend_contract_discloses_exception(monkeypatch):
    _,s,cost,now,live=candidate()
    monkeypatch.setattr(v,'trend',lambda _: 'bullish')
    assert not v.build_candidates(s,cost,now,live)[0]
    proof={'time_ms':s['15m'][-1].time_ms,'pivot_ms':s['15m'][-5].time_ms,'price':999}
    monkeypatch.setattr(v,'countertrend_break',lambda *args: proof)
    plans,_=v.build_candidates(s,cost,now,live)
    assert plans and plans[0]['evidence']['countertrend_15m_break']==proof
    p=v.apply_review({'approve':True,'candidate_id':plans[0]['candidate_id']},plans,now)
    assert p['trade_against_dominant_trend'] is True


@pytest.mark.asyncio
async def test_verified_api_receives_sources_and_persists_before_delivery(session,monkeypatch):
    from app.config import get_settings
    from app.models import AiProposedSetup, AiAnalysisRun
    from app.services import ai_ict_analysis as ai
    from sqlalchemy import select
    from app.db import SessionLocal
    from tests.test_chart_ai_pipeline import _analysis_response_for_json
    import hashlib

    monkeypatch.setenv('VERIFIED_PLANS_ENABLED','1')
    monkeypatch.setenv('AI_PROVIDER','openai_api')
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-no-network')
    get_settings.cache_clear()
    c,s,_,base,live=candidate()
    now=int(datetime.now(timezone.utc).timestamp()*1000)
    offset=now-base
    for k in ('formed_ms','expires_ms','as_of_ms'):c[k]+=offset
    for k in ('sweep_ms','reclaim_ms','swept_pivot_ms','break_ms','broken_pivot_ms','fvg_ms','poi_origin_ms'):
        if c['evidence'].get(k) is not None:c['evidence'][k]+=offset
    c['evidence']['fvg_origin_ms']=[x+offset for x in c['evidence']['fvg_origin_ms']]
    c['candidate_id']=hashlib.sha256(json.dumps({k:x for k,x in c.items() if k not in ('candidate_id','as_of_ms')},sort_keys=True).encode()).hexdigest()[:24]
    monkeypatch.setattr(v,'build_candidates',lambda *a,**kw:([c],{}))
    monkeypatch.setattr(v,'valid_bars',lambda *a,**kw:True)
    monkeypatch.setattr(ai,'build_raw_market_payload',AsyncMock(return_value=({'symbol':'BTCUSDT'},live,s)))
    evidence={'status':'available','sources':{'funding':{'rate':.0001,'source':'bitunix'}}}
    monkeypatch.setattr(ai,'load_ai_market_intelligence',AsyncMock(return_value=evidence))
    monkeypatch.setattr(ai,'build_ai_memory_context',AsyncMock(return_value={}))
    async def review(token,**kw):
        payload=json.loads(kw['user_input'])
        assert payload['market_intelligence']==evidence
        return _analysis_response_for_json({'approve':True,'candidate_id':c['candidate_id'],'reason_fa':'تست'})
    monkeypatch.setattr(ai,'stream_analysis_response',review)
    async def chart(**kw):
        async with SessionLocal() as db:
            setup=(await db.execute(select(AiProposedSetup))).scalar_one()
            assert setup.entry_price==c['entry']
            run=await db.get(AiAnalysisRun,kw['run_id'])
            assert run.status.startswith('completed')
        raise RuntimeError('synthetic chart failure after durable setup')
    monkeypatch.setattr(ai,'_generate_chart_for_run',chart)
    monkeypatch.setattr(ai,'_deliver_ai_telegram',AsyncMock(side_effect=RuntimeError('offline delivery')))
    run=await ai.run_ai_analysis(trigger='manual')
    assert run.status.startswith('completed'),run.error_detail
    async with SessionLocal() as db:
        assert len((await db.execute(select(AiProposedSetup))).scalars().all())==1
    assert 'delivery_error' in run.structured['_artifacts']['telegram']


@pytest.mark.asyncio
async def test_opportunity_summary_exposes_pre_api_blocker(session,client,monkeypatch):
    from app.models import AiAnalysisRun
    monkeypatch.setenv('VERIFIED_PLANS_ENABLED','1')
    session.add(AiAnalysisRun(trigger='scheduled',status='completed',structured={
        'verified_plan_review':{'candidate_audit':{'candidate_count':0},
                                'diagnostics':{'no_liquidity_sweep':4}}}))
    await session.commit()
    r=await client.get('/api/gui/opportunities/summary')
    assert r.status_code==200
    scan=r.json()['latest_scan']
    assert scan['candidate_count']==0 and scan['api_attempted'] is False
    assert scan['approved'] is False and scan['rejections']=={'no_liquidity_sweep':4}
