import json
import pytest
from behdark.engine import ingest, apply_quote, apply_bars, parse
from behdark.store import Store
from behdark.service import create_app
from fastapi.testclient import TestClient
NOW=1800000000
BODY='''🔴 Sell/Short signal #BTCUSDT
Entry range: 82069 _ 83547
Targets: 80353 _ 77958 _ 77213
Stop: 85072
Leverage: 8X _ 10X
Balance: 3% of capital'''
@pytest.fixture
def store(tmp_path):
    st=Store(tmp_path/'test.db')
    with st.connect() as db:
        cfg=Store.settings(db);cfg.update(capital=1000,enabled=True,activated_at=NOW-10)
        db.execute('UPDATE settings SET data=?',(json.dumps(cfg),))
        db.execute("INSERT INTO ledger(at,kind,amount) VALUES(?,'initial_capital',1000)",(NOW,))
    return st

def add(st,mid=1,body=BODY):
    with st.connect() as db:
        ingest(db,dict(key=f'-1878414449:{mid}',peer='-1878414449',body=body,received=NOW,published=NOW-1,kind='new'),NOW)

def q(st,p,t=1):
    with st.connect() as db: apply_quote(db,'BTCUSDT',p,NOW+t)

def row(st):
    with st.connect() as db:return Store.rows(db)[0]

def wallet(st):
    with st.connect() as db:return Store.wallet(db)

def test_example_two_fills_accounting_and_restart(store):
    add(store);q(store,81000)
    s=row(store)
    assert s['leverage']==10 and s['planned_margin']==30
    assert [l['margin'] for l in s['entry_legs']]==[10.5,19.5]
    assert wallet(store)['reserved']==pytest.approx(30.15)
    q(store,82069,2);s=row(store)
    assert s['margin']==10.5 and s['quantity']==pytest.approx(105/82069)
    assert s['entry_legs'][1]['status']=='pending'
    assert s['fees']==pytest.approx(.021)
    store=Store(store.path)
    q(store,83547,3);s=row(store)
    quantity=105/82069+195/83547
    assert s['quantity']==pytest.approx(quantity)
    assert s['entry']==pytest.approx(300/quantity)
    assert s['fees']==pytest.approx(.06)
    assert wallet(store)['reserved']==30
    q(store,83547,4);assert row(store)['quantity']==s['quantity']
    q(store,77213,5);s=row(store)
    expected=sum((s['entry']-tp)*quantity/3-tp*quantity/3*.0002 for tp in s['targets'])-.06
    assert s['status']=='closed' and s['pnl']==pytest.approx(expected)
    assert wallet(store)['balance']==pytest.approx(1000+expected)
    assert wallet(store)['reserved']==0

def test_target_cancels_unfilled_leg_and_stop_taker(store):
    add(store);q(store,81000);q(store,82069,2);q(store,80353,3)
    s=row(store);assert s['entry_legs'][1]['status']=='cancelled'
    assert s['remaining']==pytest.approx(2/3)
    assert wallet(store)['reserved']==pytest.approx(7)
    q(store,83547,4);assert row(store)['quantity']==pytest.approx(105/82069)
    q(store,85080,5);s=row(store)
    assert s['status']=='closed' and s['exit_fills'][-1]['fee_bps']==5
    assert wallet(store)['reserved']==0

def test_long_written_order_and_immediate_taker(store):
    body=BODY.replace('Sell/Short','Buy/Long').replace('82069 _ 83547','83547 _ 82069').replace('80353 _ 77958 _ 77213','85000 _ 86000 _ 87000').replace('Stop: 85072','Stop: 80000')
    add(store,body=body);q(store,83000)
    s=row(store)
    assert s['entry_legs'][0]['limit']==83547 and s['entry_legs'][0]['margin']==10.5
    assert s['entry']==83000 and s['entry_legs'][0]['fee_bps']==5
    assert s['entry_legs'][1]['status']=='pending'
    q(store,82069,2);assert row(store)['margin']==30

@pytest.mark.parametrize('lev',['8X _ 126X','0X _ 10X'])
def test_both_leverage_values_validated(lev):
    with pytest.raises(ValueError):parse(BODY.replace('8X _ 10X',lev))

def test_reservations_and_cancel_release(store):
    for i in range(1,4):add(store,i,BODY.replace('3%','40%'))
    q(store,81000)
    with store.connect() as db:
        rows=Store.rows(db)
        assert rows[2]['reason']=='insufficient_balance'
        assert Store.wallet(db)['reserved']==pytest.approx(804)
        rows[0]['status']='cancelled';Store.save(db,rows[0])
    q(store,81000,2)
    with store.connect() as db:assert Store.rows(db)[2]['planned_margin']==400

def test_no_retroactive_orders_and_bar_ambiguity(store):
    add(store)
    with store.connect() as db:
        apply_bars(db,'BTCUSDT',[dict(at=NOW+60,open=81000,low=81000,high=84000,close=83000)])
    assert row(store)['status']=='waiting' and 'entry_legs' not in row(store)
    q(store,81000,121)
    with store.connect() as db:
        apply_bars(db,'BTCUSDT',[dict(at=NOW+180,open=81000,low=79000,high=84000,close=83000)])
    assert row(store)['status']=='review' and row(store)['uncertain']
    assert wallet(store)['reserved']==0

def test_pause_blocks_second_leg_but_not_stop(store):
    add(store);q(store,81000);q(store,82069,2)
    with store.connect() as db:
        cfg=Store.settings(db);cfg['enabled']=False;db.execute('UPDATE settings SET data=?',(json.dumps(cfg),))
    q(store,83547,3);assert row(store)['margin']==10.5
    q(store,85072,4);assert row(store)['status']=='closed'
    assert wallet(store)['reserved']==0

def test_edit_open_cancels_remaining_entry(store):
    add(store);q(store,81000);q(store,82069,2)
    add(store,body=BODY.replace('85072','85073'))
    assert row(store)['entry_legs'][1]['status']=='cancelled'
    assert row(store)['stop']==85072

def test_settings_no_legacy_overwrite(store):
    with TestClient(create_app(store.path,background=False)) as c:
        assert c.put('/settings',json=dict(expiry_hours=0,enabled=True)).status_code==200
        assert c.get('/snapshot').json()['wallet']['margin']==20

def test_signal_size_frozen_despite_wallet_settings_changes(store):
    add(store);q(store,81000)
    with store.connect() as db:
        cfg=Store.settings(db);cfg.update(margin=800,leverage=125,fee_bps=100)
        db.execute('UPDATE settings SET data=?',(json.dumps(cfg),))
    q(store,82069,2)
    assert row(store)['margin']==10.5 and row(store)['leverage']==10
    assert row(store)['fees']==pytest.approx(.021)

def test_expiry_only_cancels_pending_leg_of_open_position(store):
    add(store);q(store,81000);q(store,82069,2)
    with store.connect() as db:
        cfg=Store.settings(db);cfg['expiry_hours']=1
        db.execute('UPDATE settings SET data=?',(json.dumps(cfg),))
    q(store,83547,3601)
    s=row(store);assert s['status']=='open' and s['entry_legs'][1]['status']=='cancelled'
    assert s['margin']==10.5
    q(store,85072,3602);assert row(store)['status']=='closed'

def test_recovered_second_entry_is_uncertain(store):
    add(store);q(store,81000);q(store,82069,2)
    with store.connect() as db:
        apply_bars(db,'BTCUSDT',[dict(at=NOW+60,open=83000,low=82500,high=84000,close=83500)])
    s=row(store);assert s['margin']==30 and s['uncertain']
