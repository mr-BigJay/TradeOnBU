"""Closed-candle context; chart-derived zones are not evidence of institutional orders."""
from app.services.ict_structure import find_swings
from app.domain.candle_quality import displacement_body


def market_zones(series):
    result = {'support_resistance': [], 'order_blocks': [],
              'definition': 'Last opposite candle before a displacement close through a confirmed swing; full candle range. Context, not entry-on-touch.'}
    for tf, bars in series.items():
        if len(bars)<20:
            continue
        pivots=find_swings(bars)
        levels=[]
        for p in pivots:
            sign=1 if p.kind=='HIGH' else -1
            if not any(sign*(b.close-p.price)>0 for b in bars[p.index+1:]):
                levels.append({'timeframe':tf,'time_ms':p.time_ms,'price':p.price,
                    'kind':'resistance' if sign==1 else 'support'})
        result['support_resistance'].extend(levels[-8:])
        blocks=[]
        for i in range(16,len(bars)):
            b=bars[i]
            ranges=[max(x.high-x.low,abs(x.high-a.close),abs(x.low-a.close)) for a,x in zip(bars[i-15:i-1],bars[i-14:i])]
            vol=sum(ranges)/len(ranges)
            for sign,direction in ((1,'LONG'),(-1,'SHORT')):
                known=[p for p in pivots if p.index+2<i and p.kind==('HIGH' if sign==1 else 'LOW')]
                if not known or sign*(b.close-known[-1].price)<=0 or sign*(bars[i-1].close-known[-1].price)>0:
                    continue
                body=displacement_body(b,sign)
                if body<vol or body<.6*(b.high-b.low):
                    continue
                origins=[j for j in range(max(0,i-6),i) if sign*(bars[j].close-bars[j].open)<0]
                if not origins:
                    continue
                j=origins[-1];origin=bars[j]
                invalid=any(sign*(x.close-(origin.low if sign==1 else origin.high))<0 for x in bars[i+1:])
                touched=any(x.low<=origin.high and x.high>=origin.low for x in bars[i+1:])
                blocks.append({'timeframe':tf,'direction':direction,'origin_ms':origin.time_ms,
                    'confirmed_ms':b.time_ms,'low':origin.low,'high':origin.high,
                    'status':'invalidated' if invalid else 'retested' if touched else 'fresh'})
        result['order_blocks'].extend(blocks[-6:])
    result['support_resistance']=result['support_resistance'][-40:]
    return result
