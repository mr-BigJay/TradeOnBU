from types import SimpleNamespace
from datetime import datetime,timezone
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select, func
from app.domain.candles import Candle
from app.models import StructureObservation, SimPosition, SimPositionSettings
from app.services import structure_shadow as sh


def fixture():
    bars=[Candle(102,103,101,102,i*sh.STEP) for i in range(18)]
    bars[3]=Candle(102,110,101,102,3*sh.STEP)
    bars[5]=Candle(102,103,90,102,5*sh.STEP)
    bars[9]=Candle(102,108,101,102,9*sh.STEP)
    bars[12]=Candle(102,103,92,102,12*sh.STEP)
    bars[14]=Candle(100,102,91,101,14*sh.STEP)
    bars[15]=Candle(101,111,100,110,15*sh.STEP)
    bars[16]=Candle(110,113,106,112,16*sh.STEP)
    bars[17]=Candle(112,114,108,113,17*sh.STEP)
    # Higher highs and higher lows before a SHORT reversal, mirrored to LONG.
    bars=[Candle(210-b.open,210-b.low,210-b.high,210-b.close,b.time_ms) for b in bars]
    return bars


def test_closed_data_and_invalid_ohlc():
    assert sh.valid_series(fixture(),sh.STEP)
    assert not sh.valid_series(fixture()[1:4]+fixture()[5:],sh.STEP)
    assert not sh.valid_series([Candle(1,2,1,3,0)],sh.STEP)


def test_review_requires_exact_unique_candidate_key():
    c=[{"key":"verified"}]
    assert sh.validate_review(c,[{"key":"invented","verdict":"watch"}])["verified"]["verdict"]=="unreviewed"
    assert sh.validate_review(c,[{"key":"verified","verdict":"watch"},{"key":"verified","verdict":"skip"}])["verified"]["verdict"]=="unreviewed"
    assert sh.validate_review(c,[{"key":"verified","verdict":"watch","reason_fa":"تأیید"}])["verified"]["verdict"]=="watch"


def test_first_touch_before_registration_is_not_fill():
    e=dict(direction="LONG",zone=[100,101],extreme=95,fvg_ms=0)
    sim=SimpleNamespace(default_margin_usdt=50,default_leverage=10,maker_fee_rate=.0002,
                        taker_fee_rate=.0005,slippage_bps=5,default_tp_behavior="market")
    bars=[Candle(103,104,102,103,0),Candle(103,104,100,102,sh.STEP)]
    state,t=sh.start(e,bars,sim,2*sh.STEP)
    assert state=="late_touch" and "fill_ms" not in t


def test_gap_and_entry_stop_are_explicit():
    costs=dict(default_margin_usdt=50,default_leverage=10,maker_fee_rate=.0002,
               taker_fee_rate=.0005,slippage_bps=5,default_tp_behavior="market")
    row=SimpleNamespace(evidence={"direction":"LONG","zone":[99,101]},state="limit",
         tracking={"entry":100,"stop":95,"tp":112,"expires_ms":sh.HOUR,"cursor":0,"costs":costs})
    sh.advance(row,[],[Candle(101,102,94,101,sh.MIN)],2*sh.MIN)
    assert row.state=="sl" and row.tracking["net_usdt"]<0
    row.state="limit";row.tracking["cursor"]=0
    sh.advance(row,[],[Candle(101,102,99,101,2*sh.MIN)],3*sh.MIN)
    assert row.state=="data_gap" and row.tracking["cursor"]==0


@pytest.mark.asyncio
async def test_journal_idempotency_without_orders(monkeypatch):
    from tests.conftest import TestSession
    from app.services.market_data import MarketDataService
    import app.db
    now=60*sh.STEP+2*sh.MIN
    monkeypatch.setattr(sh.time,"time",lambda:now/1000)
    monkeypatch.setattr(app.db,"SessionLocal",TestSession)
    import app.services.entry_strategies as strategies
    import app.services.entry_execution_settings as modes
    monkeypatch.setattr(strategies,"strategy_settings",AsyncMock(return_value={strategies.A:True}))
    monkeypatch.setattr(modes,"get_paper_entry_execution_config",AsyncMock(return_value=("STRICT",False,False)))
    five=[Candle(103,104,102,103,(60-1)*sh.STEP)]
    minute=[Candle(103,104,102,103,now-sh.MIN)]
    async def bundle(self,symbol,interval,limit):
        return SimpleNamespace(closed=five if interval=="5m" else minute)
    monkeypatch.setattr(MarketDataService,"fetch_kline_bundle",bundle)
    e=dict(key="swing:LONG:test",variant="swing",direction="LONG",zone=[100,101],extreme=95,fvg_ms=five[0].time_ms,bars=[])
    monkeypatch.setattr(sh,"detect",lambda bars,direction,variant:[e] if direction=="LONG" and variant=="swing" else [])
    async with TestSession() as s:
        s.add(SimPositionSettings(id=1))
        await s.commit()
    await sh.tick()
    await sh.tick()
    async with TestSession() as s:
        assert await s.scalar(select(func.count()).select_from(StructureObservation))==1
        assert await s.scalar(select(func.count()).select_from(SimPosition))==0


def test_exact_swing_reference_and_no_future_confirmation():
    prices=[110,115,111,108,105,108,111,107,104,100,104,108,105,102,97,100,
            105,101,98,95,99,104,100,97,93,98,102,98,95,91,96,100]
    bars=[Candle(x,x+1,x-1,x,i*sh.STEP) for i,x in enumerate(prices)]
    bars += [Candle(93,96,89,94,32*sh.STEP),Candle(94,108,93,107,33*sh.STEP),
             Candle(107,110,101,108,34*sh.STEP)]
    assert not sh.detect(bars[:-1],"LONG","swing")
    evidence=sh.detect(bars,"LONG","swing")
    assert len(evidence)==1
    e=evidence[0]
    assert e["reference_ms"]==29*sh.STEP
    assert e["pivot_ms"]==26*sh.STEP
    assert e["zone"]==[96,101] and e["origin"]==[32*sh.STEP,33*sh.STEP,34*sh.STEP]
    mirror=[Candle(210-b.open,210-b.low,210-b.high,210-b.close,b.time_ms) for b in bars]
    assert len(sh.detect(mirror,"SHORT","swing"))==1
