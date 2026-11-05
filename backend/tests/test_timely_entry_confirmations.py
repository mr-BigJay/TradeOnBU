import pytest
from app.domain.candles import Candle
from app.services.ict_confirmation_engine import process_bar_sequential
from app.services.balanced_confirmation_engine import strict_apply_5m_bar
from app.services.confirmation_policy import resolve_confirmation_policy, order_entry_allowed

STEP = 300000

@pytest.mark.parametrize('direction', ['LONG', 'SHORT'])
@pytest.mark.parametrize('token', ['fvg_retest', 'order_block_retest'])
def test_retest_and_zone_touch_complete_on_same_closed_bar(direction, token):
    sequence = ['mss', token, 'entry_zone_touch']
    bar = Candle(101, 102, 99, 101, 20*STEP)
    progress = process_bar_sequential(direction=direction, sequence=sequence,
        confirmation_state={'completed':['mss'], 'confirmation_timestamps':{'mss':19*STEP}},
        bar=bar, window=[bar], zone_lo=99, zone_hi=100,
        invalidation_level=None, structured={'ict_fvg':[{'low':99, 'high':100}]},
        monitor_from_ms=0)
    assert progress.missing == []
    assert progress.confirmation_timestamps[token] == progress.confirmation_timestamps['entry_zone_touch']
    policy = resolve_confirmation_policy(sequence=sequence)
    assert order_entry_allowed(policy, {
        'confirmation_timestamps':progress.confirmation_timestamps,
        'confirmation_evidence':progress.confirmation_evidence},
        allow_pre_entry_without_touch=False, zone_lo=99, zone_hi=100) == (True, None)


def test_retest_outside_entry_zone_does_not_complete_touch():
    bar = Candle(105, 106, 104, 105, 20*STEP)
    progress = process_bar_sequential(direction='LONG',
        sequence=['mss', 'fvg_retest', 'entry_zone_touch'],
        confirmation_state={'completed':['mss'], 'confirmation_timestamps':{'mss':19*STEP}},
        bar=bar, window=[bar], zone_lo=99, zone_hi=100, invalidation_level=None,
        structured={'ict_fvg':[{'low':104, 'high':106}]}, monitor_from_ms=0)
    assert progress.missing == ['entry_zone_touch']


def test_sweep_and_break_are_not_collapsed_into_same_bar():
    bars = [Candle(100, 101, 99, 100, i*STEP) for i in range(14)]
    bar = Candle(100, 110, 98, 109, 14*STEP)
    bars.append(bar)
    policy = resolve_confirmation_policy(sequence=['liquidity_sweep', 'mss', 'entry_zone_touch'])
    progress = strict_apply_5m_bar(direction='LONG', policy=policy,
        confirmation_state={}, bar=bar, window=bars, zone_lo=99, zone_hi=100,
        invalidation_level=90, structured={}, monitor_from_ms=0)
    assert progress.completed == ['liquidity_sweep']
    assert progress.confirmation_evidence['liquidity_sweep']['bar_low'] == 98
    assert progress.confirmation_evidence['liquidity_sweep']['bar_high'] == 110
