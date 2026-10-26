import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from app.services.market_data import MarketDataService
# Capture before the suite's autouse offline guard replaces the class method.
FETCH = MarketDataService.fetch_ticker

@pytest.mark.asyncio
@pytest.mark.parametrize('rows,valid', [
    ([{'symbol':'OTHERUSDT','lastPrice':'100'}],False),
    ([{'symbol':'TESTUSDT','lastPrice':'NaN'}],False),
    ([{'symbol':'TESTUSDT','lastPrice':'0'}],False),
    ([{'symbol':'TESTUSDT','lastPrice':'100','time':1}],False),
    ([{'symbol':'OTHERUSDT','lastPrice':'1'},{'symbol':'TESTUSDT','lastPrice':'100'}],True),
])
async def test_ticker_response(rows,valid):
    response=MagicMock(); response.json.return_value={'code':0,'data':rows}
    client=MagicMock(); client.get=AsyncMock(return_value=response)
    cm=MagicMock(); cm.__aenter__=AsyncMock(return_value=client); cm.__aexit__=AsyncMock(return_value=None)
    with patch('app.services.market_data.httpx.AsyncClient',return_value=cm):
        if valid: assert (await FETCH(MarketDataService(),'TESTUSDT'))[0]==100
        else:
            with pytest.raises(RuntimeError): await FETCH(MarketDataService(),'TESTUSDT')
