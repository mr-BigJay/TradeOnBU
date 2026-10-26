import math
import time
import httpx

class Market:
    def __init__(self,client=None):
        self.client=client or httpx.AsyncClient(base_url='https://fapi.bitunix.com',timeout=8,
                                      limits=httpx.Limits(max_connections=2))
    async def close(self): await self.client.aclose()
    async def get(self,path,params):
        r=await self.client.get('/api/v1/futures/market/'+path,params=params)
        r.raise_for_status(); body=r.json()
        if body.get('code')!=0 or not isinstance(body.get('data'),list): raise ValueError('market_response')
        return body['data']
    async def quotes(self,symbols):
        rows=await self.get('tickers',{'symbols':','.join(symbols)})
        now=time.time(); out={}
        for r in rows:
            sym=r.get('symbol'); p=float(r['lastPrice'])
            if sym not in symbols or sym in out or not math.isfinite(p) or p<=0: raise ValueError('invalid_quote')
            ts=r.get('time',r.get('ts'))
            if ts is not None and not -5<=now-float(ts)/1000<=60: raise ValueError('stale_quote')
            out[sym]=p
        return out
    async def bars(self,symbol):
        rows=await self.get('kline',{'symbol':symbol,'interval':'1m','limit':200,'type':'LAST_PRICE'})
        out=[]; now=time.time()
        for r in rows:
            b={k:float(r[k]) for k in ('open','high','low','close')}; b['at']=int(r['time'])//1000
            if not all(math.isfinite(b[k]) and b[k]>0 for k in ('open','high','low','close')): raise ValueError('invalid_candle')
            if not b['low']<=min(b['open'],b['close'])<=max(b['open'],b['close'])<=b['high']: raise ValueError('invalid_candle')
            if b['at']+60<=now: out.append(b)
        return out
