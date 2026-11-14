import json
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from app.services.daily_outlook_schema import parse_outlook, SwingPlan, validate_swing_plan
from app.services.outlook_continuity import price_progress
from app.services.outlook_telegram import format_outlook_message
from tests.test_daily_outlook import report, context


def market():
    ctx = context()
    ctx.update(analysis_mode='independent_htf_v1', daily_atr14=10, live_price=102,
               candles={'1d': [[0, 100, 120, 90, 105, 10]]}, previous_outlooks=[{'id': 7}],
               swing_cost_assumptions={'taker_fee_rate': .0006, 'maker_fee_rate': .0002, 'slippage_bps': 5, 'spread_bps': 2})
    ctx['levels'] += [{'id': 'entry', 'price': 100}, {'id': 'target', 'price': 120}]
    return ctx


def proposal(action='long'):
    return SwingPlan(action=action, rationale_fa='واکنش روزانه در حمایت معتبر بررسی شود.',
        trigger_fa='بسته شدن چهارساعته بالای حمایت و حفظ آن.', entry_level_id='entry', stop_level_id='low',
        target_level_ids=['target'], entry_valid_hours=24, holding_days=7,
        structural_reason_fa='شکست کف روزانه ساختار صعودی را باطل می‌کند.')


def test_ai_level_grounded_and_published_after_validation():
    ctx = market(); data = report()
    data['analyst_levels'] = [dict(id='ai_daily_peak', label_fa='سقف روزانه', timeframe='1d',
        candle_time_ms=0, field='high', price=120, reason_fa='این سقف مرجع بررسی شکست روزانه است.')]
    data['dominant']['target_level_ids'] = ['ai_daily_peak']
    parsed = parse_outlook(json.dumps(data), ctx)
    assert parsed['analysis_mode'] == 'independent_htf_v1'
    assert ctx['levels'][-1]['price'] == 120
    assert ctx['levels'][-1]['source'] == 'ai_selected_closed_candle'
    bad = deepcopy(data); bad['analyst_levels'][0]['price'] = 121
    untouched = market()
    with pytest.raises(ValueError, match='ungrounded_analyst_price'): parse_outlook(json.dumps(bad), untouched)
    assert all(l['id'] != 'ai_daily_peak' for l in untouched['levels'])
    bad = deepcopy(data); bad['analyst_levels'][0]['candle_time_ms'] = 1
    with pytest.raises(ValueError, match='unknown_analyst_candle'): parse_outlook(json.dumps(bad), market())


def test_reference_must_exist_and_trend_required():
    data = report(); data['previous_plan_review'] = dict(prior_report_id=8,
        assessment_fa='حرکت اخیر همچنان با سناریوی قبلی همسو است.',
        late_entry_fa='ورود تازه تنها پس از ارزیابی شرایط امروز انجام شود.')
    with pytest.raises(ValueError, match='unknown_previous_outlook'): parse_outlook(json.dumps(data), market())
    data['previous_plan_review']['prior_report_id'] = 7
    assert parse_outlook(json.dumps(data), market())['previous_plan_review']['prior_report_id'] == 7
    data['trend_assessment'] = None
    with pytest.raises(ValueError, match='missing_trend_assessment'): parse_outlook(json.dumps(data), market())


def test_swing_separate_expiry_costs_and_no_execution():
    ctx = market(); result = validate_swing_plan(proposal(), ctx['levels'], ctx)
    assert result['entry_expires_at'] == '2026-10-24T00:00:00+00:00'
    assert result['holding_days'] == 7 and result['execution_status'] == 'not_tracked'
    assert 1.5 < result['risk_reward'][0] < 2 and result['funding_included'] is False
    ctx['levels'] = [{'id': 'entry', 'price': 100}, {'id': 'low', 'price': 110}, {'id': 'target', 'price': 80}]
    ctx['live_price'] = 98
    assert validate_swing_plan(proposal('short'), ctx['levels'], ctx)['risk_reward'][0] > 1.5


@pytest.mark.parametrize('change,reason', [('tight', 'swing_stop_too_tight'), ('rr', 'swing_net_rr_below_minimum'),
    ('invalid', 'swing_already_invalidated'), ('geometry', 'invalid_swing_geometry')])
def test_bad_swing_does_not_discard_outlook(change, reason):
    ctx = market(); data = report(); data['swing_plan'] = proposal().model_dump()
    if change == 'tight': ctx['daily_atr14'] = 30
    if change == 'rr': ctx['levels'][-1]['price'] = 114
    if change == 'invalid': ctx['live_price'] = 89
    if change == 'geometry': ctx['levels'][-1]['price'] = 80
    result = parse_outlook(json.dumps(data), ctx)
    assert result['summary_fa'] == data['summary_fa'] and result['swing_plan']['action'] == 'wait'
    assert result['swing_plan']['validation_reason'] == reason and 'entry_price' not in result['swing_plan']


def test_price_progress_ignores_pre_report_bars_and_never_claims_fill():
    old = dict(as_of='1970-01-01T01:00:00+00:00', dominant={'target_level_ids':['t']}, levels=[{'id':'t','price':110}])
    ctx = {'candles': {'4h': [[0,100,120,90,100], [14400000,100,109,95,105], [28800000,105,112,100,110]]}}
    result = price_progress(old, ctx)
    assert result['bar_count'] == 2 and result['targets'][0]['first_touch_open_ms'] == 28800000
    assert result['unobserved_initial_ms'] == 10800000 and result['execution_status'] == 'not_tracked'
    assert price_progress({}, ctx)['status'] == 'unavailable'


def test_telegram_independent_report_has_continuity_and_swing_not_paper_status():
    ctx = market(); data = report(); data['swing_plan'] = proposal().model_dump()
    data['previous_plan_review'] = dict(prior_report_id=7, assessment_fa='سناریوی قبلی هنوز مشروط به حفظ حمایت معتبر است.',
        late_entry_fa='ورود تازه باید براساس شرایط جدید دوباره بررسی شود.')
    parsed = parse_outlook(json.dumps(data), ctx)
    row = SimpleNamespace(report=parsed, context=ctx, symbol='BTCUSDT', id=8, slot_close_ms=0)
    message = format_outlook_message(row, datetime.now(timezone.utc), 'https://example.test')
    assert 'پیگیری سناریوی قبلی' in message and 'اعتبار ورود' in message and 'فاندینگ آینده لحاظ نشده' in message
    assert 'سفارش آزمایشی' not in message and len(message) < 4000

@pytest.mark.asyncio
async def test_daily_pipeline_remembers_history_without_calling_entry_engine(monkeypatch):
    from datetime import timedelta
    from unittest.mock import AsyncMock
    from tests.conftest import TestSession
    from app.models import DailyOutlookRun
    from app.services import daily_outlook as service
    monkeypatch.setattr(service, 'SessionLocal', TestSession)
    now = datetime.now(timezone.utc)
    oldstamp = now-timedelta(days=1)
    oldctx = market(); oldctx['as_of'] = oldstamp.isoformat()
    async with TestSession() as session:
        old = DailyOutlookRun(symbol='BTCUSDT', report_date=oldstamp.date().isoformat(),
            slot_close_ms=0, trigger='manual', status='completed', started_at=oldstamp,
            completed_at=oldstamp, context=oldctx, report=report())
        session.add(old); await session.commit(); old_id=old.id
        current = DailyOutlookRun(symbol='BTCUSDT', report_date=now.date().isoformat(),
            slot_close_ms=0, trigger='manual', status='running', started_at=now, context={})
        session.add(current); await session.commit(); current_id=current.id
    ctx = market(); ctx['as_of'] = now.isoformat()
    generated = report(); generated['previous_plan_review'] = dict(prior_report_id=old_id,
        assessment_fa='سناریوی قبلی با قیمت فعلی دوباره بررسی شده است.',
        late_entry_fa='ورود جدید نیاز به حفظ حمایت و شرایط تازه دارد.')
    engine = AsyncMock(side_effect=AssertionError('daily outlook must not depend on engine'))
    monkeypatch.setattr(service,'snapshot_plan',engine)
    monkeypatch.setattr(service,'build_daily_context',AsyncMock(return_value=ctx))
    monkeypatch.setattr(service,'load_ai_market_intelligence',AsyncMock(return_value={}))
    monkeypatch.setattr(service,'provider_credentials',AsyncMock(return_value=('test','test-model','high')))
    infer=AsyncMock(return_value=SimpleNamespace(text=json.dumps(generated),response_metadata={}))
    monkeypatch.setattr(service,'stream_analysis_response',infer)
    monkeypatch.setattr(service,'deliver_outlook',AsyncMock())
    await service._work(current_id)
    supplied=json.loads(infer.await_args.kwargs['user_input'])['market']
    assert supplied['previous_outlooks'][0]['id']==old_id
    assert 'price_progress' in supplied['previous_outlooks'][0]
    assert 'poi_context' not in supplied and 'strategy_features' not in supplied
    engine.assert_not_awaited()
    async with TestSession() as session:
        saved=await session.get(DailyOutlookRun,current_id)
        assert saved.status=='completed'
        assert saved.report['previous_plan_review']['prior_report_id']==old_id
        assert saved.context['response_archive']['execution_plan'] is None
