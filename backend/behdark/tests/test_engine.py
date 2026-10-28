import json
import pytest
from fastapi.testclient import TestClient
from behdark.store import Store
from behdark.engine import ingest,apply_quote,apply_bars,parse
from behdark.service import create_app,report,Runner

BODY='''🟢 Buy/Long Signal #TESTUSDT
Entry range: 99 - 101
Targets: 110 - 120
Stop: 95
Leverage: 3X - 4X
Balance: 2% of capital'''
NOW=1800000000.0

def event(mid=1,body=BODY,at=NOW):
    return dict(key=f'-1878414449:{mid}',peer='-1878414449',body=body,received=at,published=at-1,kind='new')

@pytest.fixture
def store(tmp_path):
    s=Store(tmp_path/'independent.db')
    with s.connect() as db:
        cfg=s.settings(db);cfg.update(capital=1000,enabled=True,activated_at=NOW-10)
        db.execute('UPDATE settings SET data=?',(json.dumps(cfg),))
        db.execute("INSERT INTO ledger(at,kind,amount) VALUES(?,'initial_capital',1000)",(NOW,))
    return s

def row(store):
    with store.connect() as db: return Store.rows(db)[0]

def quote(store,p,at=NOW+1):
    with store.connect() as db: apply_quote(db,'TESTUSDT',p,at)

def add(store,e=None,now=NOW):
    with store.connect() as db:
        ingest(db,e or event(),now)
        for s in Store.rows(db):
            s.pop('execution_version',None)
            Store.save(db,s)

def test_persistent_dedup_and_partial_targets(store):
    add(store);add(store);quote(store,100)
    assert row(store)['status']=='open'
    with store.connect() as db:
        assert len(Store.rows(db))==1
        assert Store.wallet(db)['reserved']==20
    quote(store,111,NOW+5)
    assert row(store)['remaining']==pytest.approx(.5)
    quote(store,111,NOW+10)
    assert row(store)['remaining']==pytest.approx(.5)
    quote(store,121,NOW+15)
    r=row(store); assert r['status']=='closed'; assert r['target_hits']==[0,1]
    assert r['pnl']==pytest.approx(9-.036-.0198-.0216)
    with store.connect() as db:
        assert Store.wallet(db)['reserved']==0
        assert Store.wallet(db)['balance']==pytest.approx(1000+r['pnl'])
    assert Store(store.path).path==store.path
    assert report([r])['winrate']==100

def test_short_and_stop(store):
    body=BODY.replace('Buy/Long','Sell/Short').replace('110 - 120','90 - 80').replace('Stop: 95','Stop: 105')
    add(store,event(body=body));quote(store,100);quote(store,90,NOW+5);quote(store,107,NOW+10)
    r=row(store); assert r['status']=='closed'; assert r['target_hits']==[0]
    assert r['exit_fills'][-1]['price']==107
    assert r['pnl']==pytest.approx(3-2.1-.036-.0162-.01926)

def test_wait_longer_than_15_minutes_and_no_capital_isolated(store):
    add(store);quote(store,105)
    assert row(store)['status']=='waiting'
    quote(store,100,NOW+4000)
    assert row(store)['status']=='open'
    assert row(store)['uncertain']

def test_pause_only_blocks_new_entries(store):
    add(store);quote(store,100)
    with store.connect() as db:
        cfg=Store.settings(db);cfg['enabled']=False;db.execute('UPDATE settings SET data=?',(json.dumps(cfg),))
    quote(store,94,NOW+5)
    assert row(store)['status']=='closed'

def test_insufficient_funds_recorded(store):
    with store.connect() as db:
        cfg=Store.settings(db);cfg['margin']=2000;db.execute('UPDATE settings SET data=?',(json.dumps(cfg),))
    add(store);quote(store,100)
    assert row(store)['status']=='waiting'; assert row(store)['reason']=='insufficient_balance'
    with store.connect() as db: assert Store.wallet(db)['balance']==1000

def test_settings_freeze_and_separate_signals_same_symbol(store):
    add(store);quote(store,100)
    with store.connect() as db:
        cfg=Store.settings(db);cfg.update(margin=50,leverage=5);db.execute('UPDATE settings SET data=?',(json.dumps(cfg),))
    add(store,event(2));quote(store,100,NOW+5)
    with store.connect() as db:
        rows=Store.rows(db); assert len(rows)==2
        assert rows[0]['margin']==20 and rows[0]['leverage']==3
        assert rows[1]['margin']==50 and rows[1]['leverage']==5

def test_cutoff_late_edit_and_wrong_peer(store):
    add(store,event(at=NOW-100),NOW)
    with store.connect() as db: assert not Store.rows(db)
    e=event();e['peer']='-999';add(store,e)
    with store.connect() as db: assert not Store.rows(db)
    add(store,event(),NOW+121);assert row(store)['reason']=='late_receipt'

def test_edit_pending_requires_review(store):
    add(store);e=event();e.update(kind='edited',body=BODY.replace('Stop: 95','Stop: 94'));add(store,e)
    r=row(store);assert r['status']=='review';assert r['stop']==95;assert len(r['revisions'])==1
    add(store,e);assert len(row(store)['revisions'])==1

def test_edit_open_preserves_original_exit(store):
    add(store);quote(store,100)
    e=event();e.update(kind='edited',body=BODY.replace('Stop: 95','Stop: 90'));add(store,e)
    quote(store,94,NOW+5);r=row(store)
    assert r['status']=='closed' and r['uncertain'];assert report([r])['winrate'] is None

def test_candle_ambiguity_never_claims_win(store):
    add(store)
    with store.connect() as db:
        apply_bars(db,'TESTUSDT',[dict(at=NOW+60,open=104,high=112,low=94,close=106)])
    assert row(store)['status']=='review';assert row(store)['uncertain']

def test_open_same_bar_stop_and_target_is_conservative(store):
    add(store);quote(store,100)
    with store.connect() as db:
        apply_bars(db,'TESTUSDT',[dict(at=NOW+60,open=100,high=112,low=94,close=106)])
    r=row(store);assert r['status']=='closed';assert r['pnl']<0;assert r['uncertain']

def test_bar_margin_exhaustion_uses_reached_level(store):
    with store.connect() as db:
        cfg=Store.settings(db);cfg['leverage']=100;db.execute('UPDATE settings SET data=?',(json.dumps(cfg),))
    add(store);quote(store,100)
    with store.connect() as db: apply_bars(db,'TESTUSDT',[dict(at=NOW+60,open=100,high=100.5,low=98,close=99)])
    r=row(store);assert r['status']=='closed';assert r['exit_fills'][0]['price']==99
    assert r['close_reason']=='margin_exhausted'

def test_no_main_app_import():
    from pathlib import Path
    for p in Path(__file__).parents[1].glob('*.py'):
        assert 'from app.' not in p.read_text()
        assert 'import app.' not in p.read_text()

@pytest.mark.parametrize('body',[BODY.replace('99 - 101','NaN - 101'),BODY+'\nextra',BODY.replace('Stop: 95','Stop: 130')])
def test_reject_bad_template(body):
    with pytest.raises(ValueError): parse(body)

def test_api_initialization_atomic_and_config(tmp_path):
    app=create_app(tmp_path/'behdark.db',background=False)
    with TestClient(app) as c:
        cfg=dict(margin=10,leverage=3,fee_bps=6,expiry_hours=0,enabled=True)
        assert c.put('/settings',json=cfg).status_code==409
        assert c.post('/capital',json={'amount':1000}).status_code==200
        assert c.post('/capital',json={'amount':1000}).status_code==409
        assert c.put('/settings',json=cfg).status_code==200
        first=c.get('/snapshot').json()['wallet']['activated_at']
        assert c.put('/settings',json={**cfg,'leverage':126}).status_code==422
        assert c.put('/settings',json=cfg).status_code==200
        assert c.get('/snapshot').json()['wallet']['activated_at']==first
        assert c.get('/report.csv').status_code==200

@pytest.mark.asyncio
async def test_collection_stops_and_reader_disconnect_keeps_exits(store,tmp_path,monkeypatch):
    monkeypatch.setattr("behdark.service.time.time",lambda:NOW+10)
    from unittest.mock import AsyncMock
    m=AsyncMock();m.bars.return_value=[];m.quotes.return_value={'TESTUSDT':94}
    add(store);quote(store,100)
    # No feed heartbeat: existing paper position still exits using its own prices.
    r=Runner(store,tmp_path,market=m)
    await r.tick()
    assert row(store)['status']=='closed'
    m.reset_mock();await r.tick();m.quotes.assert_not_called();m.bars.assert_not_called()

def test_unsupported_and_reply_are_visible(store):
    e=event(body='image only');add(store,e)
    assert row(store)['status']=='review'

@pytest.mark.asyncio
async def test_market_rejects_stale_or_wrong_symbol():
    import httpx,time
    from behdark.market import Market
    m=Market(client=httpx.AsyncClient(trust_env=False));await m.client.aclose()
    payload={'code':0,'data':[{'symbol':'OTHERUSDT','lastPrice':'100'}]}
    async def respond(request): return httpx.Response(200,json=payload)
    m.client=httpx.AsyncClient(base_url='https://fapi.bitunix.com',transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(ValueError): await m.quotes(['TESTUSDT'])
        payload['data']=[{'symbol':'TESTUSDT','lastPrice':'100','time':(time.time()-120)*1000}]
        with pytest.raises(ValueError): await m.quotes(['TESTUSDT'])
        payload['data']=[{'symbol':'TESTUSDT','lastPrice':'100'}]
        assert await m.quotes(['TESTUSDT'])=={'TESTUSDT':100}
    finally: await m.close()

@pytest.mark.asyncio
async def test_market_rejects_forming_and_invalid_bars():
    import httpx,time
    from behdark.market import Market
    m=Market(client=httpx.AsyncClient(trust_env=False));await m.client.aclose();now=int(time.time())
    candle=dict(time=now*1000,open='100',high='101',low='99',close='100')
    async def respond(request): return httpx.Response(200,json={'code':0,'data':[candle]})
    m.client=httpx.AsyncClient(base_url='https://fapi.bitunix.com',transport=httpx.MockTransport(respond))
    try:
        assert await m.bars('TESTUSDT')==[]
        candle['time']=(now-120)*1000
        assert len(await m.bars('TESTUSDT'))==1
        candle['low']='NaN'
        with pytest.raises(ValueError): await m.bars('TESTUSDT')
    finally: await m.close()
