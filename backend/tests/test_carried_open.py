from dataclasses import replace
import pytest
from app.domain.candles import Candle
from app.domain.candle_quality import carried_open, acceptable_prices, displacement_body
from app.services.verified_trade_plan import valid_bars

# Actual Bitunix responses for 2026-11-05; raw extremes must be preserved.
PREV=Candle(79696.7,79703.1,79655.7,79676.2,1793885700000)
BAR=Candle(79676.2,79718.4,79678.6,79700,1793885760000)

def test_evidence_and_raw_extremes():
    before=BAR
    assert carried_open(BAR,PREV,60000)
    assert valid_bars([PREV,BAR],60000,BAR.time_ms+60000,2)
    assert BAR==before
    assert BAR.low > BAR.open  # no fabricated entry touches below low
    assert displacement_body(BAR,1)==pytest.approx(21.4)
    assert displacement_body(BAR,1)<BAR.close-BAR.open

@pytest.mark.parametrize('prev,bar',[
    (None,BAR), (replace(PREV,time_ms=PREV.time_ms-60000),BAR),
    (replace(PREV,close=79676.1),BAR),
    (PREV,replace(BAR,close=80000)),(PREV,replace(BAR,high=79000)),
    (PREV,replace(BAR,open=float('nan'))),
])
def test_bad_data_still_rejected(prev,bar):
    assert not acceptable_prices(bar,prev,60000)

def test_bearish_gap_not_displacement():
    b=Candle(110,105,100,103,120000)
    p=Candle(109,112,108,110,60000)
    assert carried_open(b,p,60000)
    assert displacement_body(b,-1)==2
    assert displacement_body(b,1)==-2

def test_trimmed_window_requires_ingestion_evidence():
    assert not valid_bars([BAR],60000,BAR.time_ms+60000,1)
    verified=replace(BAR,carried_open_verified=carried_open(BAR,PREV,60000))
    assert valid_bars([verified],60000,BAR.time_ms+60000,1)
    assert not acceptable_prices(replace(verified,close=80000))

@pytest.mark.asyncio
async def test_http_parser_preserves_prices_and_attaches_evidence(monkeypatch):
    import httpx
    from app.services.market_data import MarketDataService
    class Resp:
        def raise_for_status(self): pass
        def json(self):
            return {'code':0,'data':[{'time':str(b.time_ms),'open':str(b.open),'high':str(b.high),'low':str(b.low),'close':str(b.close)} for b in [BAR,PREV]]}
    class Client:
        def __init__(self,**kw): pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,*args,**kw):return Resp()
    monkeypatch.setattr(httpx,'AsyncClient',Client)
    bars=await MarketDataService()._fetch_klines_window('BTCUSDT','1m')
    assert not bars[0].carried_open_verified
    assert bars[1].carried_open_verified
    assert replace(bars[1],carried_open_verified=False)==replace(BAR,volume=None)
