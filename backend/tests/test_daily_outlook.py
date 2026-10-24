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
    return dict(trade_plan=dict(action='wait', rationale_fa='سطوح برای نسبت سود به زیان کافی نیست.', trigger_fa='منتظر تشکیل ساختار معتبر بمانید.', entry_level_id=None, stop_level_id=None, target_level_ids=[]), summary_fa='ساختار بازار برای چند روز آینده به تأیید شکست و حفظ سطح وابسته است.',
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
@pytest.mark.parametrize("action", ["wait", "long", "short"])
async def test_success_and_usage_without_trade_writes(isolated,monkeypatch,action):
    snapshot = context() if action == "wait" else plan_context(action)
    generated = report() if action == "wait" else planned_report(action)
    monkeypatch.setattr(service,'build_daily_context',AsyncMock(return_value=snapshot))
    monkeypatch.setattr(service,'load_ai_market_intelligence',AsyncMock(return_value={}))
    monkeypatch.setattr(service,'provider_credentials',AsyncMock(return_value=('test','test-model','high')))
    inference=AsyncMock(return_value=SimpleNamespace(text=json.dumps(generated),response_metadata={'usage':{'input_tokens':123}}))
    monkeypatch.setattr(service,'stream_analysis_response',inference)
    run_id,owner,_=await service.reserve_run('manual')
    await service.execute_reserved(run_id,owner)
    data=await service.dashboard()
    assert data['latest']['status']=='completed'
    assert data['latest']['usage']['input_tokens']==123
    assert data['latest']['report']['trade_plan']['action'] == action
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


@pytest.mark.asyncio
@pytest.mark.parametrize('recent,escapes_daily,should_fail',[(False,False,False),(True,False,True),(False,True,True)])
async def test_historical_body_anomaly_only_allowed_inside_valid_daily_range(recent,escapes_daily,should_fail):
    now=int(datetime(2026,10,23,11,tzinfo=timezone.utc).timestamp()*1000)
    bars=[bar(t) for t in range(now//DAY*DAY-90*DAY, now//(4*HOUR)*(4*HOUR),4*HOUR)]
    i=-5 if recent else 122
    # Internal constituent: aggregated daily O/C alone would not detect this.
    bars[i]=Candle(time_ms=bars[i].time_ms,open=85 if escapes_daily else 100,high=110,low=104.6,close=105,volume=10)
    market=SimpleNamespace(_now_ms=lambda:now,
        fetch_historical_closed=AsyncMock(return_value=SimpleNamespace(closed=bars,data_complete=True)),
        fetch_live_price=AsyncMock(return_value=SimpleNamespace(price=105)),is_quote_stale=lambda q:False)
    if should_fail:
        with pytest.raises(ValueError,match='invalid_candle_range'):
            await build_daily_context('BTCUSDT',market)
    else:
        ctx=await build_daily_context('BTCUSDT',market)
        assert len(ctx['data_quality_warnings'])==1
        assert bars[i].time_ms not in [b[0] for b in ctx['candles']['4h']]
        assert all(b[3]==90 for b in ctx['candles']['1d'])


@pytest.mark.asyncio
async def test_pre_ai_failure_can_retry_once_and_preserves_audit(isolated):
    rid,_,_=await service.reserve_run('manual')
    async with TestSession() as s:
        row=await s.get(DailyOutlookRun,rid)
        row.status='failed';row.error_code='invalid_candle_range'
        cfg=await s.get(DailyOutlookSettings,1)
        cfg.enabled=True;cfg.hour=0;cfg.minute=0
        await s.commit()
    assert (await service.dashboard())['can_retry_today'] is True
    assert (await service.reserve_run('scheduled'))[2]=='already_attempted_today'
    rid2,owner,state=await service.reserve_run('manual')
    assert rid2==rid and state=='started'
    assert (await service.reserve_run('manual'))[2]=='already_attempted_today'
    async with TestSession() as s:
        row=await s.get(DailyOutlookRun,rid)
        assert row.error_code is None and row.context['pre_ai_failures'][0]['error_code']=='invalid_candle_range'
        assert await s.scalar(select(func.count()).select_from(DailyOutlookRun))==1


@pytest.mark.parametrize('field,value',[('model_slug','test'),('usage',{'input_tokens':1}),('context',{'ai_request_started':True}),('error_code','analysis_timeout')])
def test_possible_paid_requests_never_retry(field,value):
    row=SimpleNamespace(status='failed',error_code='invalid_candle_range',model_slug=None,usage={},context={})
    setattr(row,field,value)
    assert not service.retryable(row)


def test_chart_path_uses_only_known_levels_and_ends_at_target():
    result=report();result['dominant']['path_level_ids']=['low','high']
    assert parse_outlook(json.dumps(result),context())['dominant']['path_level_ids']==['low','high']
    result['dominant']['path_level_ids']=['imaginary','high']
    with pytest.raises(ValueError,match='unknown_evidence_level'):parse_outlook(json.dumps(result),context())
    result['dominant']['path_level_ids']=['high','low']
    with pytest.raises(ValueError,match='invalid_chart_path'):parse_outlook(json.dumps(result),context())


def test_serialize_returns_original_4h_snapshot_without_fetch():
    row=DailyOutlookRun(id=9,symbol='BTCUSDT',report_date='2026-10-23',trigger='manual',status='completed',started_at=datetime.now(timezone.utc),context={'candles':{'4h':[[1,2,3,1,2]],'1d':[]}})
    assert service.serialize_run(row)['context']['four_hour_candles']==[[1,2,3,1,2]]


def image_context():
    ctx = context()
    end = int(datetime(2026, 10, 23, tzinfo=timezone.utc).timestamp()*1000)
    ctx.update(live_price=105, candles={'4h': [[end-(120-i)*4*HOUR, 100+i*.02, 110+i*.02, 90+i*.02, 105+i*.02] for i in range(120)]})
    return ctx


def test_static_chart_preserves_ohlc_and_uses_saved_levels():
    from app.services.daily_outlook_image import chart_snapshot
    ctx = image_context()
    result = chart_snapshot(ctx, report())
    assert result['bars'] == ctx['candles']['4h']
    assert result['paths'][0]['points'] == [(0, 105), (7, 110)]
    assert result['anchor'].isoformat() == '2026-10-23T00:00:00+00:00'
    ctx['candles']['4h'][-1][3] = 106
    with pytest.raises(ValueError, match='invalid_snapshot_candle'):
        chart_snapshot(ctx, report())


def test_static_chart_png_and_daily_fallback():
    from app.services.daily_outlook_image import render_report_image, chart_snapshot
    ctx = image_context()
    png = render_report_image(json.dumps(dict(id=1, symbol='BTCUSDT', date='2026-10-23', context=ctx, report=report())))
    assert png.startswith(b'\x89PNG\r\n\x1a\n') and len(png) > 20000
    ctx['candles'] = {'1d': [[int(datetime(2026,10,22,tzinfo=timezone.utc).timestamp()*1000),100,110,90,105]]}
    assert chart_snapshot(ctx, report())['step'] == 86400


def test_static_chart_excludes_forming_candles_and_unknown_paths():
    from app.services.daily_outlook_image import chart_snapshot
    ctx = image_context()
    ctx['candles']['4h'].append([int(datetime(2026,10,23,tzinfo=timezone.utc).timestamp()*1000),100,110,90,105])
    r = report()
    r['dominant']['path_level_ids'] = ['unknown']
    result = chart_snapshot(ctx, r)
    assert len(result['bars']) == 119  # last 120 includes the rejected forming bar
    assert [p['kind'] for p in result['paths']] == ['alternative']


@pytest.mark.asyncio
async def test_timeline_pagination_and_chart_endpoint(isolated, client):
    async with TestSession() as s:
        for i in range(32):
            s.add(DailyOutlookRun(symbol='BTCUSDT', report_date=(datetime(2026,9,1)+timedelta(days=i)).date().isoformat(),
                trigger='manual',status='completed',started_at=datetime.now(timezone.utc),
                context=image_context(),report=report()))
        await s.commit()
    first = (await client.get('/api/gui/daily-outlook/runs')).json()
    assert len(first['items']) == 30 and first['next_cursor']
    assert 'report' not in first['items'][0]
    second = (await client.get('/api/gui/daily-outlook/runs', params={'before_id':first['next_cursor']})).json()
    assert len(second['items']) == 2 and second['next_cursor'] is None
    assert not ({r['id'] for r in first['items']} & {r['id'] for r in second['items']})
    rid = first['items'][0]['id']
    image = await client.get(f'/api/gui/daily-outlook/runs/{rid}/chart-image')
    assert image.status_code == 200
    assert image.json()['data_url'].startswith('data:image/png;base64,iVBOR')
    assert (await client.get('/api/gui/daily-outlook/runs/999/chart-image')).status_code == 404
    async with TestSession() as s:
        row = await s.get(DailyOutlookRun,rid)
        row.status = 'running'
        await s.commit()
    assert (await client.get(f'/api/gui/daily-outlook/runs/{rid}/chart-image')).status_code == 409


def planned_report(action='long'):
    result = report()
    result['trade_plan'] = dict(action=action, rationale_fa='ورود مشروط بر بازآزمایی سطح معتبر است.',
        trigger_fa='بسته شدن کندل چهار ساعته و بازآزمایی سطح ورود',
        entry_level_id='entry', stop_level_id='stop', target_level_ids=['tp1', 'tp2'])
    return result


def plan_context(action='long'):
    prices = [100, 90, 120, 130] if action == 'long' else [100, 110, 80, 70]
    return {**context(), 'levels': context()['levels'] + [
        {'id': id, 'price': value} for id, value in zip(['entry', 'stop', 'tp1', 'tp2'], prices)]}


@pytest.mark.parametrize('action', ['long', 'short'])
def test_plan_resolves_prices_and_computes_rr(action):
    result = parse_outlook(json.dumps(planned_report(action)), plan_context(action))['trade_plan']
    assert result['entry_price'] == 100
    assert result['risk_reward'] == [2, 3]
    assert result['take_profits'] == ([120, 130] if action == 'long' else [80, 70])


@pytest.mark.parametrize('field,value,error', [
    ('entry_level_id', 'invented', 'unknown_trade_plan_level'),
    ('stop_level_id', 'entry', 'invalid_trade_plan_geometry'),
    ('stop_level_id', 'tp1', 'invalid_trade_plan_geometry'),
    ('target_level_ids', ['stop'], 'invalid_trade_plan_geometry'),
    ('target_level_ids', ['tp2', 'tp1'], 'invalid_trade_plan_target_order'),
    ('target_level_ids', ['high'], 'trade_plan_rr_below_two'),
    ('target_level_ids', [], 'incomplete_trade_plan'),
])
def test_plan_rejects_unsafe_or_invented_levels(field, value, error):
    result = planned_report()
    result['trade_plan'][field] = value
    with pytest.raises(ValueError, match=error):
        parse_outlook(json.dumps(result), plan_context())


def test_wait_plan_has_no_prices_and_new_report_requires_plan():
    result = report()
    assert parse_outlook(json.dumps(result), context())['trade_plan']['action'] == 'wait'
    result['trade_plan']['entry_level_id'] = 'high'
    with pytest.raises(ValueError, match='wait_plan_must_not_have_prices'):
        parse_outlook(json.dumps(result), context())
    del result['trade_plan']
    with pytest.raises(ValueError):
        parse_outlook(json.dumps(result), context())
