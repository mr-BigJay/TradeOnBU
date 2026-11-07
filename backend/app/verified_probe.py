"""Read-only deployment probe: public Bitunix data + stored Paper costs. No AI/order calls."""
import asyncio
import json
import time
from app.domain.candle_quality import acceptable_prices
from app.db import SessionLocal
from app.models import SimPositionSettings
from app.services.market_data import MarketDataService
from app.services.verified_trade_plan import enabled, valid_bars, build_candidates, VERSION


async def main():
    market=MarketDataService();series={};quality={}
    # Capture one as-of boundary for every interval to avoid mixing forming bars.
    now=int(time.time()*1000)
    for tf,step in (('1m',60000),('5m',300000),('15m',900000),('1h',3600000),('4h',14400000)):
        bundle=await market.fetch_kline_bundle('BTCUSDT',tf,120)
        bars=[b for b in bundle.closed if b.time_ms+step<=now]
        series[tf]=bars
        bad=[b.time_ms for i,b in enumerate(bars) if not acceptable_prices(b, bars[i-1] if i else None, step)]
        carried=[b.time_ms for b in bars if b.carried_open_verified]
        quality[tf]={'usable':bundle.data_complete and valid_bars(bars,step,now,30),
            'carried_open_count':len(carried),'carried_open_samples':carried[:5],
            'count':len(bars),'invalid_ohlc_samples':bad[:5],'invalid_ohlc_count':len(bad),
            'gaps_remaining':len(bundle.gaps_remaining or [])}
    quote=await market.fetch_live_price('BTCUSDT')
    from app.services import multi_setup_analyst as analyst
    if analyst.enabled():
        try:
            evidence=analyst.catalog(series,now)
            ok=all(q['usable'] for q in quality.values()) and not market.is_quote_stale(quote)
            print(json.dumps({'status':'data_ok' if ok else 'data_blocked','version':analyst.VERSION,
                'enabled':True,'paper_only':True,'quality':quality,'evidence_count':len(evidence),
                'note':'Evidence areas are not signals. API proposes scenarios every four hours; monitor confirms entry.'},ensure_ascii=False,indent=2))
            return 0 if ok else 2
        except ValueError as exc:
            print(json.dumps({'status':'data_blocked','reason':str(exc),'quality':quality}));return 2
    async with SessionLocal() as session:
        costs=await session.get(SimPositionSettings,1)
        if costs is None:
            print(json.dumps({'status':'paper_settings_missing','quality':quality}));return 2
        audit={}
        candidates,reasons=build_candidates(series,costs,now,quote.price,audit=audit)
    ok=all(q['usable'] for q in quality.values()) and not market.is_quote_stale(quote)
    print(json.dumps({'status':'data_ok' if ok else 'data_blocked','version':VERSION,'enabled':enabled(),
        'paper_only':True,'quality':quality,'candidate_count':len(candidates),'rejections':reasons,'candidate_audit':audit},ensure_ascii=False,indent=2))
    return 0 if ok else 2

if __name__=='__main__':raise SystemExit(asyncio.run(main()))
