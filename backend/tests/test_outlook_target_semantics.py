import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.services.daily_outlook_schema import parse_outlook
from app.services.daily_outlook_image import chart_snapshot
from app.services.daily_outlook_data import build_daily_context, DAY, HOUR
from tests.test_daily_outlook import report, bar
from tests.test_outlook_target_chart import snapshot


def test_saved_failure_text_lengths_are_accepted_without_truncation():
    ctx, r = snapshot()
    r['uncertainty_fa'] = 'الف ' * 75
    r['change_since_previous_fa'] = 'ب ' * 125
    parsed = parse_outlook(json.dumps(r), ctx)
    assert parsed['uncertainty_fa'] == r['uncertainty_fa']
    assert parsed['change_since_previous_fa'] == r['change_since_previous_fa']


def test_bearish_retest_above_quote_is_not_a_target_or_target_arrow():
    ctx, r = snapshot()
    r['dominant'].update(direction='bearish', activation_level_ids=['high'],
        retest_level_ids=['high'], target_level_ids=['high'],
        path_level_ids=['upper', 'high'], target_zone_level_ids=[])
    r['alternative']['direction'] = 'bullish'
    parsed = parse_outlook(json.dumps(r), ctx)
    s = parsed['dominant']
    assert s['target_level_ids'] == [] and s['target_validation'] == 'rejected_geometry'
    assert s['retest_level_ids'] == ['high']
    chart = chart_snapshot(ctx, parsed)
    assert all(p['kind'] != 'dominant' for p in chart['paths'])
    assert chart['retests'] == [{'kind': 'dominant', 'price': 110}]


def test_missing_destination_allowed_with_reason_but_not_fake_path():
    ctx, r = snapshot()
    r['dominant'].update(target_level_ids=[], path_level_ids=[], target_zone_level_ids=[],
        target_unavailable_reason_fa='هدف تاریخی معتبری در داده‌ها موجود نیست.')
    assert parse_outlook(json.dumps(r), ctx)['dominant']['target_level_ids'] == []
    r['dominant']['path_level_ids'] = ['low', 'high']
    with pytest.raises(ValueError, match='targetless_scenario'):
        parse_outlook(json.dumps(r), ctx)


def test_target_beyond_quote_but_before_activation_is_rejected():
    ctx, r = snapshot()
    r['dominant']['activation_level_ids'] = ['upper']
    assert parse_outlook(json.dumps(r), ctx)['dominant']['target_level_ids'] == []


@pytest.mark.asyncio
async def test_extended_daily_history_and_actual_counts():
    now = int(datetime(2026,10,23,11,tzinfo=timezone.utc).timestamp()*1000)
    bars = [bar(t) for t in range(now//DAY*DAY-370*DAY, now//(4*HOUR)*(4*HOUR),4*HOUR)]
    market = SimpleNamespace(_now_ms=lambda: now,
        fetch_historical_closed=AsyncMock(return_value=SimpleNamespace(closed=bars, data_complete=True)),
        fetch_live_price=AsyncMock(return_value=SimpleNamespace(price=105)), is_quote_stale=lambda q:False)
    ctx = await build_daily_context('BTCUSDT', market, extended=True)
    assert len(ctx['candles']['1d']) == 365
    assert len(ctx['candles']['1w']) == 52
    assert len(ctx['candles']['4h']) == 120
    assert ctx['history_coverage']['daily_count'] == 365
    assert market.fetch_historical_closed.call_args.kwargs['max_bars'] == 2400
