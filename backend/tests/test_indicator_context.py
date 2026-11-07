import math
from dataclasses import replace
import pytest
from app.domain.candles import Candle
from app.services.strategy_features import _rsi_series, _ema, closed_bar_features, strategy_features


def bars():
    return [Candle(100+i,102+i,99+i,101+i,i*300000,10) for i in range(60)]


def test_wilder_published_reference_and_seed():
    # Wilder's commonly reproduced worksheet; independent known RSI values.
    prices=[44.34,44.09,44.15,43.61,44.33,44.83,45.10,45.42,45.84,46.08,45.89,46.03,45.61,46.28,46.28,46.00,46.03,46.41,46.22,45.64,46.21]
    values=_rsi_series(prices)
    assert values[0]==pytest.approx(70.464135,abs=1e-5)
    assert values[1]==pytest.approx(66.249619,abs=1e-5)
    assert values[-1]==pytest.approx(62.880718,abs=1e-5)
    assert _ema(list(range(1,31)),9)==pytest.approx(26)
    assert _ema(list(range(1,31)),20)==pytest.approx(20.5)


def test_extremes_flat_and_directional_information():
    assert _rsi_series(list(range(40)))[-1]==100
    assert _rsi_series(list(range(40,0,-1)))[-1]==0
    assert _rsi_series([100]*40)[-1] is None
    f=closed_bar_features(bars())
    assert f['ema9_change']==pytest.approx(1)
    assert f['ema20_change']==pytest.approx(1)
    assert f['ema9_ema20_relation']=='above' and f['ema9_ema20_cross']=='none'
    assert f['rsi14']==100 and f['rsi14_change']==0
    assert len(f['rsi14_recent'])==5 and f['rsi14_recent'][-1]['open_ms']==bars()[-1].time_ms


def test_new_cross_and_missing_history():
    rows=[Candle(100,100,100,100,i*300000,1) for i in range(40)]
    assert closed_bar_features(rows)['ema9_ema20_relation']=='equal'
    rows[-1]=replace(rows[-1],high=110,close=110)
    assert closed_bar_features(rows)['ema9_ema20_cross']=='up'
    rows[-1]=replace(rows[-1],high=100,low=90,close=90)
    assert closed_bar_features(rows)['ema9_ema20_cross']=='down'
    assert not closed_bar_features(rows[:20])['available']
    rows[-1]=replace(rows[-1],close=float('nan'))
    assert not closed_bar_features(rows)['available']


def test_all_execution_timeframes_and_gaps_are_descriptive_only():
    series={tf:[replace(b,time_ms=i*step) for i,b in enumerate(bars())] for tf,step in [('5m',300000),('15m',900000),('1h',3600000),('4h',14400000)]}
    f=strategy_features(series)
    assert set(f['timeframes'])==set(series)
    assert f['mode']=='descriptive_only_no_entry_gate'
    assert all(x['rsi14']==100 for x in f['timeframes'].values())
    series['15m'].pop(-2)
    assert strategy_features(series)['timeframes']['15m']['reason']=='non_contiguous_closed_history'
