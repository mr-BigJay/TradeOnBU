from dataclasses import replace
import pytest
from app.domain.candles import Candle
from app.services import verified_trade_plan as v
from app.services.poi_context import market_zones
from tests.test_verified_trade_plan import fixture


def ob_fixture():
    s,cost,_,_=fixture()
    s['5m']=s['5m'][:57]
    now=s['5m'][-1].time_ms+v.STEP
    for tf,step in [('15m',900000),('1h',3600000)]:
        s[tf]=[replace(x,low=960) for x in s[tf] if x.time_ms+step<=now]
    s['5m'][-1]=replace(s['5m'][-1],close=995.95,low=995.8)
    return s,cost,now,995.95


@pytest.mark.parametrize('mirror',[False,True])
def test_order_block_entry_needs_no_future_fvg_candle(mirror):
    s,cost,now,live=ob_fixture()
    if mirror:
        s={tf:[Candle(2000-x.open,2000-x.low,2000-x.high,2000-x.close,x.time_ms) for x in bars] for tf,bars in s.items()}
        live=2000-live
    cs,reasons=v.build_candidates(s,cost,now,live)
    assert cs,reasons
    c=cs[0]
    assert c['evidence']['poi_kind']=='OB'
    assert c['formed_ms']==now
    assert c['evidence']['fvg_ms'] is None
    plan=v.apply_review({'approve':True,'candidate_id':c['candidate_id']},cs,now)
    assert plan['entry_poi_type']=='OB' and v.plan_error(plan,now) is None
    assert c['direction']==('LONG' if mirror else 'SHORT')


def test_order_block_needs_displacement_and_unused_zone():
    s,cost,now,live=ob_fixture()
    c=v.build_candidates(s,cost,now,live)[0][0]
    s['5m'].append(Candle(live,c['zone'][1]+.1,live-.1,c['entry'],now))
    assert not v.build_candidates(s,cost,now+v.STEP,c['entry'])[0]
    s,cost,now,live=ob_fixture()
    s['5m'][-1]=replace(s['5m'][-1],open=996)
    assert not v.build_candidates(s,cost,now,live)[0]


def test_context_marks_retested_and_invalidated_blocks():
    s,_,now,live=ob_fixture()
    bars=s['5m']
    fresh=[b for b in market_zones({'5m':bars})['order_blocks'] if b['confirmed_ms']==bars[-1].time_ms]
    assert fresh and fresh[-1]['status']=='fresh'
    ob=fresh[-1]
    touch=Candle(live,ob['low']+.1,live-.1,ob['low'],now)
    blocks=market_zones({'5m':bars+[touch]})['order_blocks']
    assert next(b for b in blocks if b['origin_ms']==ob['origin_ms'])['status']=='retested'
    invalid=Candle(live,ob['high']+1,live-.1,ob['high']+.5,now)
    blocks=market_zones({'5m':bars+[invalid]})['order_blocks']
    assert next(b for b in blocks if b['origin_ms']==ob['origin_ms'])['status']=='invalidated'


def test_higher_timeframe_support_resistance_is_not_truncated_by_lower_frames():
    s,_,_,_=fixture()
    s={'4h':s['1h'],**s}
    zones=market_zones(s)['support_resistance']
    assert {'4h','1h','15m'} <= {z['timeframe'] for z in zones}


@pytest.mark.parametrize('direction,stop,obstacle',[('LONG',990,1050),('SHORT',1010,950)])
def test_net_floor_includes_costs_and_uses_first_tick(direction,stop,obstacle):
    from app.services.ict_execution_plan import target_at_net_rr
    from app.services.execution_model_sim import auto_paper_rr_from_settings
    from app.domain.paper_execution_policy import HARD_MIN_AUTO_PAPER_NET_RR
    _,cost,_,_=fixture()
    assert HARD_MIN_AUTO_PAPER_NET_RR==1.4
    sign=1 if direction=='LONG' else -1
    target=target_at_net_rr(cost,direction,1000,stop,obstacle)
    def rr(tp):
        return auto_paper_rr_from_settings(cost,direction=direction,entry=1000,sl=stop,tp=tp,min_net_rr=1.4)
    assert rr(target).acceptable
    assert not rr(target-sign*.1).acceptable
    assert not rr(1000+sign*14).acceptable  # Gross 1.4 is below the net floor.


def test_invalid_higher_timeframe_never_supplies_an_obstacle():
    s,cost,now,live=ob_fixture()
    s['4h']=s['1h']  # Incorrect timestamps/cadence.
    assert v.build_candidates(s,cost,now,live)[1]=={'invalid_or_stale_4h':1}
