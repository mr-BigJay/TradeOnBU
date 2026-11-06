from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest
from app.services import verified_trade_plan as v
from app.verified_audit import evaluate, history
from tests.test_verified_trade_plan import fixture


def test_direction_exclusion_is_not_reported_as_found_setup(monkeypatch):
    series,cost,now,live = fixture()
    monkeypatch.setattr(v,'trend',lambda _: 'bullish')
    audit={}
    v.build_candidates(series,cost,now,live,audit=audit)
    short = audit['directions']['SHORT']
    assert short['status'] == 'excluded_before_pattern_search'
    assert short['stages'] == {}
    assert short['samples']['opposing_hourly_structure'][0]['sweep_ms'] is None
    assert audit['directions']['LONG']['stages']['sweep_bars_examined'] > 0
    assert audit['directions']['LONG']['reasons']


def test_audit_does_not_change_candidates_or_input_and_samples_bounded():
    series,cost,now,live = fixture()
    before=deepcopy(series)
    expected,_=v.build_candidates(series,cost,now,live)
    audit={}
    actual,reasons=v.build_candidates(series,cost,now,live,audit=audit)
    assert actual == expected and actual
    assert series == before
    assert audit['candidate_count'] == len(actual)
    assert audit['rejection_counts'] == reasons
    assert audit['directions']['SHORT']['stages']['candidate_built'] > 0
    assert all(len(samples)<=3 for d in audit['directions'].values() for samples in d['samples'].values())


def test_flat_bars_explain_missing_sweep():
    series,cost,now,live=fixture()
    series['5m']=[replace(b,open=1000,high=1000,low=1000,close=1000) for b in series['5m']]
    audit={}
    plans,reasons=v.build_candidates(series,cost,now,1000,audit=audit)
    assert not plans
    assert reasons['no_liquidity_sweep'] > 0


@pytest.mark.asyncio
async def test_history_native_pagination_excludes_future_and_refuses_stuck_page():
    series,_,now,_=fixture()
    bars=series['5m']
    market=SimpleNamespace(_fetch_klines_window=AsyncMock(return_value=bars))
    got=await history(market,'5m',bars[0].time_ms,now-v.STEP)
    assert got[-1].time_ms+v.STEP<=now-v.STEP
    market._fetch_klines_window.return_value=[replace(bars[-1],time_ms=now)]
    with pytest.raises(ValueError,match='history_not_advancing'):
        await history(market,'5m',bars[0].time_ms,now)


def test_reconstruction_blocks_missing_intervals_not_false_no_candidate():
    series,cost,now,live=fixture()
    result=evaluate(series,cost,now,{tf:120 for tf in ('1m','5m','15m','1h','4h')})
    assert result['status']=='data_blocked'
    assert not result['quality']['1m']

@pytest.mark.asyncio
async def test_recorded_audit_uses_read_only_database_without_network(tmp_path,monkeypatch):
    import sqlite3,json
    from app import verified_audit as cli
    path=tmp_path/'source.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE ai_analysis_runs (id INTEGER, created_at TEXT, completed_at TEXT, validation TEXT, structured TEXT)')
        db.execute('INSERT INTO ai_analysis_runs VALUES (1,?,?,?,?)',('2026-11-06 08:11:20','2026-11-06 08:11:22',json.dumps({'candidate_audit':{'version':1,'directions':{}}}),'{}'))
        db.execute('CREATE TABLE sim_position_settings (id INTEGER,default_margin_usdt REAL,default_leverage INTEGER,maker_fee_rate REAL,taker_fee_rate REAL,slippage_bps REAL,default_tp_behavior TEXT)')
        db.execute("INSERT INTO sim_position_settings VALUES (1,50,3,.0002,.0005,1,'market')")
    before=path.read_bytes()
    def no_network():
        raise AssertionError('Recorded audits must not fetch market data')
    monkeypatch.setattr(cli,'MarketDataService',no_network)
    result=await cli.run(SimpleNamespace(db=str(path),first_run=1,last_run=1))
    assert result['runs'][0]['status']=='recorded_audit'
    assert before==path.read_bytes()


def test_reconstruction_excludes_future_candles(monkeypatch):
    from app import verified_audit as cli
    from app.domain.candles import Candle
    now=3_600_000_000
    limits={tf:40 for tf in cli.STEPS}
    series={tf:[Candle(100,101,99,100,now//step*step-(40-i)*step)
                for i in range(42)] for tf,step in cli.STEPS.items()}
    seen={}
    def detector(windows,cost,as_of,live,*,audit):
        seen.update(windows)
        assert as_of==now and live==100
        return [],{}
    monkeypatch.setattr(cli,'build_candidates',detector)
    assert cli.evaluate(series,SimpleNamespace(),now,limits)['status']=='reconstructed'
    assert all(len(bars)==40 and all(b.time_ms+cli.STEPS[tf]<=now for b in bars)
               for tf,bars in seen.items())
