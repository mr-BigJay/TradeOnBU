import time
from unittest.mock import AsyncMock, patch
import pytest
from sqlalchemy import select
from app.db import SessionLocal, init_db
from app.models import TelegramPaperEvent, SimPosition, RuntimeSettings
from app.services.telegram_paper import parse_signal, process_event, RangeMarket
from app.services.position import wallet_service as wallet

BODY = '''🟢 Buy/Long Signal #TESTUSDT
Entry range: 99 - 101
Targets: 110 - 115
Stop: 96
Leverage: 3X - 4X
Balance: 2% of capital'''

def event():
    now=time.time()
    return dict(key='-1878414449:123', peer='-1878414449', body=BODY, kind='new', published=now-5, received=now)

def test_parse():
    s=parse_signal(BODY)
    assert s['symbol']=='TESTUSDT' and s['leverage']==3
    assert s['low']==99 and s['take_profit']==110
    s=parse_signal(BODY.replace('Buy/Long','Sell/Short').replace('110 - 115','90 - 85').replace('Stop: 96','Stop: 104'))
    assert s['direction']=='SHORT'

@pytest.mark.parametrize('body', [BODY+'\nUpdate: cancelled',BODY.replace('99 - 101','NaN - 101'),BODY.replace('Stop: 96','Stop: 105'),BODY.replace('#TESTUSDT','#TEST'), 'quoted signal\n'+BODY])
def test_reject_ambiguous(body):
    with pytest.raises(ValueError): parse_signal(body)

async def seed(session):
    await wallet.append_ledger(session,entry_type=wallet.LEDGER_INITIAL,amount_usdt=1000)
    session.add(RuntimeSettings(id=1, paper_trading_paused=False, max_simultaneous_positions=3, min_net_rr=2))
    await session.commit()

@pytest.mark.asyncio
async def test_order_atomic_duplicate_and_symbol():
    await init_db()
    async with SessionLocal() as s:
        await seed(s)
        with patch.object(RangeMarket, 'fetch_ticker', AsyncMock(return_value=(100,None))) as ticker:
            async with s.begin_nested(): await process_event(s,event())
            await s.commit()
            async with s.begin_nested(): await process_event(s,event())
            await s.commit()
        positions=list((await s.scalars(select(SimPosition))).all())
        assert len(positions)==1
        p=positions[0]
        assert p.symbol=='TESTUSDT' and p.status=='open' and p.leverage==3 and p.margin_usdt==20
        assert p.extra['source_label']=='BehDark'
        assert (await s.get(TelegramPaperEvent,'-1878414449:123')).status=='opened'
        assert all(c.args[0]=='TESTUSDT' for c in ticker.call_args_list)

@pytest.mark.asyncio
@pytest.mark.parametrize('change,reason', [({'kind':'baseline'},'edited_or_baseline'),({'kind':'edited'},'edited_or_baseline'),({'published':1},'stale_message'),({'received':1,'published':1},'stale_message'),({'body':'Update: stopped'},'unsupported_or_invalid_signal')])
async def test_no_orders_for_history_and_updates(change,reason):
    await init_db()
    async with SessionLocal() as s:
        e=event(); e.update(change)
        await process_event(s,e); await s.commit()
        assert (await s.get(TelegramPaperEvent,e['key'])).reason==reason
        assert not list((await s.scalars(select(SimPosition))).all())

@pytest.mark.asyncio
async def test_changed_quote_rolls_back_order_and_reserve():
    await init_db()
    async with SessionLocal() as s:
        await seed(s)
        before=await wallet.wallet_summary(s)
        with patch.object(RangeMarket,'fetch_ticker',AsyncMock(side_effect=[(100,None),(100,None),ValueError('outside_entry_range')])):
            async with s.begin_nested(): await process_event(s,event())
            await s.commit()
        assert not list((await s.scalars(select(SimPosition))).all())
        after=await wallet.wallet_summary(s)
        assert after['available_balance']==before['available_balance']
        assert (await s.get(TelegramPaperEvent,'-1878414449:123')).status=='waiting'

@pytest.mark.asyncio
async def test_pause():
    await init_db()
    async with SessionLocal() as s:
        await seed(s)
        r=await s.get(RuntimeSettings,1); r.paper_trading_paused=True; await s.commit()
        async with s.begin_nested(): await process_event(s,event())
        assert (await s.get(TelegramPaperEvent,'-1878414449:123')).reason=='paper_trading_paused'

@pytest.mark.asyncio
async def test_multi_symbol_monitor_fetches_each_symbol():
    from app.services.position.position_monitor import SimPositionMonitorService
    from app.services.market_data import MarketDataService, KlineBundle
    from app.models import SimPosition
    await init_db()
    async with SessionLocal() as s:
        for i,symbol in enumerate(['BTCUSDT','TESTUSDT']):
            s.add(SimPosition(public_id=str(i), symbol=symbol, direction='LONG', status='open',
                order_type='market', margin_usdt=10, leverage=2, entry_price=100, stop_loss=90,
                take_profit=130, extra={}))
        await s.commit()
        market=MarketDataService()
        bundle=KlineBundle(closed=[],forming=None,gaps_detected=[],gaps_remaining=[],data_complete=True,interval='1m')
        market.fetch_kline_bundle=AsyncMock(return_value=bundle)
        with patch('app.services.position.position_monitor.record_equity_snapshot',AsyncMock()):
            await SimPositionMonitorService(market).monitor_session(s)
        assert [c.args[0] for c in market.fetch_kline_bundle.call_args_list]==['BTCUSDT','TESTUSDT']

@pytest.mark.asyncio
async def test_export_file_duplicate_after_restart(tmp_path,monkeypatch):
    import json
    from app.services.telegram_paper import telegram_paper_tick
    await init_db()
    monkeypatch.setenv('TELEGRAM_PAPER_ENABLED','1')
    monkeypatch.setenv('TELEGRAM_PAPER_INBOX',str(tmp_path))
    (tmp_path/'_feed.json').write_text(json.dumps({'heartbeat':time.time()}))
    (tmp_path/'signal.json').write_text(json.dumps(event()))
    async with SessionLocal() as s: await seed(s)
    with patch.object(RangeMarket,'fetch_ticker',AsyncMock(return_value=(100,None))):
        await telegram_paper_tick()
        await telegram_paper_tick()
    async with SessionLocal() as s:
        assert len(list((await s.scalars(select(SimPosition))).all()))==1

@pytest.mark.asyncio
async def test_disabled_integration_does_not_open(tmp_path,monkeypatch):
    from app.services.telegram_paper import telegram_paper_tick
    monkeypatch.delenv('TELEGRAM_PAPER_ENABLED',raising=False)
    with patch.object(RangeMarket,'fetch_ticker',AsyncMock()) as ticker:
        await telegram_paper_tick()
        ticker.assert_not_called()

@pytest.mark.asyncio
async def test_quote_past_stop_is_terminal():
    from app.services.market_data import MarketDataService
    with patch.object(MarketDataService,'fetch_ticker',AsyncMock(return_value=(95,None))):
        with pytest.raises(ValueError,match='signal_already_invalidated'):
            await RangeMarket(parse_signal(BODY)).fetch_ticker('TESTUSDT')
