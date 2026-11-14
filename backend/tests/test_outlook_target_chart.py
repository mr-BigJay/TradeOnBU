import json
import pytest
from app.services.daily_outlook_image import chart_snapshot, render_report_image
from app.services.daily_outlook_schema import parse_outlook
from tests.test_daily_outlook import image_context, report, DAY


def snapshot():
    ctx = image_context()
    ctx['analysis_mode'] = 'independent_htf_v1'
    end = ctx['candles']['4h'][-1][0] + 4 * 3600000
    ctx['candles']['1d'] = [[end-(30-i)*DAY, 100, 112, 89, 105] for i in range(30)]
    ctx['levels'].append({'id': 'upper', 'price': 112})
    data = report()
    data['dominant']['activation_level_ids'] = ['low']
    data['dominant']['target_zone_level_ids'] = ['high', 'upper']
    return ctx, data


def test_daily_chart_uses_daily_bars_and_exact_grounded_target_zone():
    ctx, data = snapshot()
    parsed = parse_outlook(json.dumps(data), ctx)
    chart = chart_snapshot(ctx, parsed)
    assert chart['bars'] == ctx['candles']['1d']
    assert chart['step'] == 86400
    assert chart['channel']['available'] is False
    assert chart['target_zones'] == [{'kind': 'dominant', 'low': 110, 'high': 112}]
    assert chart['paths'][0]['points'][-1][1] == 110
    assert render_report_image(json.dumps(dict(id=1, date='2026-10-23', context=ctx, report=parsed))).startswith(b'\x89PNG')


@pytest.mark.parametrize('ids', [['high'], ['upper', 'high'], ['high', 'high'], ['low', 'low'], ['missing', 'upper']])
def test_invalid_or_ungrounded_zone_rejected(ids):
    ctx, data = snapshot()
    data['dominant']['target_zone_level_ids'] = ids
    with pytest.raises(ValueError):
        parse_outlook(json.dumps(data), ctx)


def test_zone_must_contain_final_destination():
    ctx, data = snapshot()
    data['dominant']['target_zone_level_ids'] = ['low', 'high']
    data['dominant']['target_level_ids'] = ['upper']
    with pytest.raises(ValueError, match='invalid_target_zone'):
        parse_outlook(json.dumps(data), ctx)


def test_no_invented_band_for_legacy_report_or_single_target():
    ctx, data = snapshot()
    del data['dominant']['target_zone_level_ids']
    assert chart_snapshot(ctx, data)['target_zones'] == []
    ctx.pop('analysis_mode')
    assert chart_snapshot(ctx, data)['step'] == 14400
