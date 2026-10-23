import json
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, func
from tests.conftest import TestSession
from app.domain.candles import Candle
from app.models import DailyOutlookRun, DailyOutlookSettings
from app.services import daily_outlook as service
from app.services.daily_outlook_data import aggregate_complete, validate_bars, build_daily_context, DAY, HOUR
from app.services.daily_outlook_schema import parse_outlook


def bar(t):
    return Candle(time_ms=t, open=100, high=110, low=90, close=105, volume=10)


def report():
    scenario = dict(title_fa='سناریوی مشروط', direction='bullish', thesis_fa='ساختار بازار تنها پس از تأیید معتبر است.',
        activation_fa='بسته شدن روزانه بالای سقف روز قبل', invalidation_fa='بسته شدن روزانه زیر کف روز قبل',
        activation_level_ids=['high'], invalidation_level_ids=['low'], target_level_ids=['high'],
        path_fa=['ابتدا تأیید شکست سقف', 'سپس بررسی حفظ سطح در بازآزمایی'])
    return dict(summary_fa='ساختار بازار برای چند روز آینده به تأیید شکست و حفظ سطح وابسته است.',
        weekly_context_fa='ساختار هفتگی نیاز به تأیید دارد.', daily_structure_fa='ساختار روزانه در محدوده است.',
        dominant=scenario, alternative={**scenario, 'direction':'bearish'},
        uncertainty_fa='داده خبری در این گزارش وجود ندارد.', watch_next_fa=['بسته شدن روزانه'],
        change_since_previous_fa='گزارش قبلی وجود ندارد.')


def context():
    return {'levels':[{'id':'high','price':110}, {'id':'low','price':90}], 'candles':{}, 'as_of':'2026-10-23T00:00:00Z'}


def test_partial_days_and_monday_weeks():
    monday = int(datetime(2026,10,12,tzinfo=timezone.utc).timestamp()*1000)
    bars = [bar(t) for t in range(monday, monday+8*DAY, 4*HOUR)]
    days = aggregate_complete(bars, days=1, now_ms=monday+7*DAY+HOUR)
    assert len(days)==7
    assert days[0].volume==60
    weeks = aggregate_complete(days, days=7, now_ms=monday+7*DAY+HOUR)
    assert len(weeks)==1 and weeks[0].time_ms==monday and weeks[0].volume==420
    assert aggregate_complete(days[:-1], days=7, now_ms=monday+8*DAY)==[]
    assert len(aggregate_complete(bars[1:],days=1,now_ms=monday+8*DAY))==7


@pytest.mark.parametrize('bars,now,error', [
    ([bar(0),bar(8*HOUR)],12*HOUR,'market_history_gap'),
    ([bar(0)],8*HOUR,'latest_closed_candle_missing'),
    ([Candle(100,90,80,105,0)],4*HOUR,'invalid_candle_range'),
])
def test_bad_history_rejected(bars,now,error):
    with pytest.raises(ValueError,match=error):
        validate_bars(bars,step=4*HOUR,now_ms=now,minimum=1)


@pytest.mark.asyncio
async def test_full_context_closed_only_and_stale_quote():
    now=int(datetime(2026,10,23,11,tzinfo=timezone.utc).timestamp()*1000)
    bars=[bar(t) for t in range(now//DAY*DAY-90*DAY, now//(4*HOUR)*(4*HOUR),4*HOUR)]
    market=SimpleNamespace(_now_ms=lambda:now,
        fetch_historical_closed=AsyncMock(return_value=SimpleNamespace(closed=bars,data_complete=True)),
        fetch_live_price=AsyncMock(return_value=SimpleNamespace(price=105)),is_quote_stale=lambda q:False)
    ctx=await build_daily_context('BTCUSDT',market)
    assert len(ctx['candles']['1d'])==80 and len(ctx['candles']['1w'])>=10
    assert ctx['candles']['1d'][-1][0] == now//DAY*DAY-DAY
    assert len(ctx['levels'])==10
    market.is_quote_stale=lambda q:True
    with pytest.raises(ValueError,match='stale_live_quote'):
        await build_daily_context('BTCUSDT',market)


def test_schema_rejects_invented_levels_and_duplicate_direction():
    result=report()
    assert parse_outlook(json.dumps(result),context())['dominant']['direction']=='bullish'
    result['dominant']['target_level_ids']=['invented']
    with pytest.raises(ValueError,match='unknown_evidence_level'):
        parse_outlook(json.dumps(result),context())
    result=report(); result['alternative']['direction']='bullish'
    with pytest.raises(ValueError): parse_outlook(json.dumps(result),context())


def test_tehran_schedule_boundary():
    now=datetime(2026,10,23,4,34,59,tzinfo=timezone.utc)
    assert not service.due(now,8,5)
    assert service.due(now+timedelta(seconds=1),8,5)
    assert service.scheduled_time(datetime(2026,10,23,21,tzinfo=timezone.utc),8,5).day==24


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(service, 'SessionLocal', TestSession)
    monkeypatch.setattr(service, 'acquire_analysis_lock', AsyncMock(return_value=True))
    monkeypatch.setattr(service, 'release_analysis_lock', AsyncMock())
    from app.routes import daily_outlook as route
    monkeypatch.setattr(route, 'SessionLocal', TestSession)


@pytest.mark.asyncio
async def test_disabled_schedule_busy_and_single_attempt(isolated):
    assert (await service.reserve_run('scheduled'))[2]=='not_due'
    service.acquire_analysis_lock.return_value=False
    assert (await service.reserve_run('manual'))[2]=='analysis_busy'
    service.acquire_analysis_lock.return_value=True
    run_id, owner, state=await service.reserve_run('manual')
    assert state=='started'
    assert (await service.reserve_run('manual'))[2]=='already_attempted_today'
    async with TestSession() as s:
        row=await s.get(DailyOutlookRun,run_id)
        row.status='failed'
        await s.commit()
    assert (await service.reserve_run('manual'))[2]=='already_attempted_today'
    async with TestSession() as s:
        assert (await s.scalar(select(func.count()).select_from(DailyOutlookRun)))==1


@pytest.mark.asyncio
async def test_success_and_usage_without_trade_writes(isolated,monkeypatch):
    monkeypatch.setattr(service,'build_daily_context',AsyncMock(return_value=context()))
    monkeypatch.setattr(service,'load_ai_market_intelligence',AsyncMock(return_value={}))
    monkeypatch.setattr(service,'provider_credentials',AsyncMock(return_value=('test','test-model','high')))
    inference=AsyncMock(return_value=SimpleNamespace(text=json.dumps(report()),response_metadata={'usage':{'input_tokens':123}}))
    monkeypatch.setattr(service,'stream_analysis_response',inference)
    run_id,owner,_=await service.reserve_run('manual')
    await service.execute_reserved(run_id,owner)
    data=await service.dashboard()
    assert data['latest']['status']=='completed'
    assert data['latest']['usage']['input_tokens']==123
    assert data['attempted_today'] is True
    inference.assert_awaited_once()
    service.release_analysis_lock.assert_awaited_once_with(owner)
    from app.models import AiAnalysisRun, AiProposedSetup
    async with TestSession() as s:
        assert await s.scalar(select(func.count()).select_from(AiAnalysisRun))==0
        assert await s.scalar(select(func.count()).select_from(AiProposedSetup))==0


@pytest.mark.asyncio
async def test_failed_data_never_calls_ai_and_releases(isolated,monkeypatch):
    monkeypatch.setattr(service,'build_daily_context',AsyncMock(side_effect=ValueError('market_history_gap')))
    inference=AsyncMock()
    monkeypatch.setattr(service,'stream_analysis_response',inference)
    run_id,owner,_=await service.reserve_run('manual')
    await service.execute_reserved(run_id,owner)
    data=await service.dashboard()
    assert data['latest']['error_code']=='market_history_gap'
    inference.assert_not_awaited()
    service.release_analysis_lock.assert_awaited_once_with(owner)


@pytest.mark.asyncio
async def test_read_access_and_admin_schedule(isolated,client,admin_client):
    assert (await client.get('/api/gui/daily-outlook')).status_code==200
    assert (await client.post('/api/gui/daily-outlook/generate')).status_code==401
    assert (await client.put('/api/gui/daily-outlook/settings',json={'enabled':True,'hour':8,'minute':5})).status_code==401
    r=await admin_client.put('/api/gui/daily-outlook/settings',json={'enabled':True,'hour':8,'minute':5})
    assert r.status_code==200 and r.json()['settings']['enabled'] is True
    assert (await admin_client.put('/api/gui/daily-outlook/settings',json={'enabled':True,'hour':24,'minute':5})).status_code==422



@pytest.mark.asyncio
async def test_scheduled_retry_after_busy_but_not_after_attempt(isolated):
    async with TestSession() as s:
        s.add(DailyOutlookSettings(id=1,enabled=True,hour=0,minute=0))
        await s.commit()
    service.acquire_analysis_lock.return_value=False
    assert (await service.reserve_run('scheduled'))[2]=='analysis_busy'
    service.acquire_analysis_lock.return_value=True
    assert (await service.reserve_run('scheduled'))[2]=='started'
    assert (await service.reserve_run('scheduled'))[2]=='already_attempted_today'


@pytest.mark.asyncio
async def test_invalid_model_report_keeps_usage(isolated,monkeypatch):
    monkeypatch.setattr(service,'build_daily_context',AsyncMock(return_value=context()))
    monkeypatch.setattr(service,'load_ai_market_intelligence',AsyncMock(return_value={}))
    monkeypatch.setattr(service,'provider_credentials',AsyncMock(return_value=('test','test-model','high')))
    monkeypatch.setattr(service,'stream_analysis_response',AsyncMock(return_value=SimpleNamespace(text='{}',response_metadata={'usage':{'input_tokens':123}})))
    run_id,owner,_=await service.reserve_run('manual')
    await service.execute_reserved(run_id,owner)
    data=await service.dashboard()
    assert data['latest']['status']=='failed' and data['latest']['usage']['input_tokens']==123


def test_hourly_hotfix_manual_does_not_shift_boundary(monkeypatch):
    from app.services import ai_analysis_scheduler as scheduler
    from app.models import AiAnalysisSettings
    now=datetime(2026,10,23,11,35,tzinfo=timezone.utc)
    cfg=AiAnalysisSettings(interval_minutes=60,next_run_at=datetime(2026,10,23,12,1,tzinfo=timezone.utc))
    monkeypatch.setattr(scheduler,'_utc_now',lambda:now)
    before=cfg.next_run_at
    scheduler.record_run_outcome(cfg,'completed',trigger='manual')
    assert cfg.next_run_at==before
    scheduler.record_run_outcome(cfg,'completed',trigger='scheduled')
    assert cfg.next_run_at==before
    assert scheduler._next_regular_run_at(cfg,before)==datetime(2026,10,23,13,1,tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_closed_setup_not_parallel_blocker(monkeypatch):
    from app.services.ai_setup_quality_gates import _open_same_direction_setups
    from app.services import ai_setup_memory
    rows=[SimpleNamespace(direction='SHORT',setup_id='closed',lifecycle_state='POSITION_CLOSED'),
          SimpleNamespace(direction='SHORT',setup_id='active',lifecycle_state='WAIT_SHORT')]
    monkeypatch.setattr(ai_setup_memory,'load_open_setups',AsyncMock(return_value=rows))
    assert [s.setup_id for s in await _open_same_direction_setups(None,symbol='BTCUSDT',direction='SHORT')]==['active']
