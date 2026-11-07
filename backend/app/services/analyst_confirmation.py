"""Replay only recent closed evidence; never replay a fill before registration."""
from app.services.ict_confirmation_engine import process_bar_sequential

STEP = 300000
STEPS = {'5m':STEP, '15m':900000, '1h':3600000, '4h':14400000}


def recent_confirmations(contract, bars, observed_ms):
    sequence = {'ict_reversal':['liquidity_sweep','mss'],
                'trend_continuation':['trend_pullback','trend_resume'],
                'breakout_retest':['breakout_close','breakout_retest']}[contract['strategy_id']]
    # A pivot needs two right-hand candles. Never validate an older event with
    # a reference that was only discoverable later.
    known=[]
    for key in ('zone','invalidation'):
        area=contract['evidence'][key]
        known.append(area.get('confirmed_ms',area['time_ms']+3*STEPS[area['timeframe']]))
    reference=contract.get('confirmation_reference')
    if reference:known.append(reference['break_confirmed_ms'])
    start=max(observed_ms//STEP*STEP-3*STEP,*known)
    state={}
    closed=[b for b in bars if b.time_ms+STEP<=observed_ms]
    for i,bar in enumerate(closed):
        if bar.time_ms<start:continue
        if i and bar.time_ms-closed[i-1].time_ms!=STEP:return {}
        if state.get('confirmation_timestamps') and 'target' in contract:
            reached=bar.high>=contract['target'] if contract['direction']=='LONG' else bar.low<=contract['target']
            if reached:return {}
        if len(state.get('confirmation_timestamps',{}))==len(sequence):
            # A subsequent visit already spent this entry. No retroactive fill.
            if bar.low<=contract['entry'] if contract['direction']=='LONG' else bar.high>=contract['entry']:
                return {}
        progress=process_bar_sequential(direction=contract['direction'],sequence=sequence,
            confirmation_state=state,bar=bar,window=closed[:i+1],zone_lo=contract['zone'][0],
            zone_hi=contract['zone'][1],invalidation_level=contract['invalidation'],
            structured={'verified_trade_plan':contract,'breakout_level':contract['breakout_level']},monitor_from_ms=start)
        if progress.invalidated:return {}
        state={'confirmation_timestamps':progress.confirmation_timestamps,
               'confirmation_evidence':progress.confirmation_evidence}
    # Incomplete historical patterns remain forward-only; no piecemeal hindsight.
    return state if len(state.get('confirmation_timestamps',{}))==len(sequence) else {}
