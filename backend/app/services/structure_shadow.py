"""A read-only market observer with its own journal. No order or wallet access."""
import math
import os
import time
from types import SimpleNamespace
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from app.models import StructureObservation, SimPositionSettings
from app.services.ict_execution_plan import STEP, structural_target, target_at_two_r
from app.services.ict_structure import find_swings, DISPLACEMENT_BODY_RATIO
from app.services.ict_confirmation_engine import _token_on_bar
MIN = 60000
HOUR = 60 * MIN
ACTIVE = ('await_touch', 'limit', 'filled')

def enabled():
    return os.getenv('ICT_STRUCTURE_SHADOW') == '1'

def valid_series(bars, step):
    return all((math.isfinite(x) and x > 0 for bar in bars for x in (bar.open, bar.high, bar.low, bar.close))) and all((bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high for bar in bars)) and all((bar.time_ms % step == 0 for bar in bars)) and all((bar.time_ms - a.time_ms == step for a, bar in zip(bars, bars[1:])))

def detect(bars, direction, variant):
    """Scan closed bars. A pivot is known only after its two right-hand candles."""
    if len(bars) < 24 or not valid_series(bars, STEP):
        return []
    long = direction == 'LONG'
    sign = 1 if long else -1
    swings = find_swings(bars)
    found = []
    for sweep_index in range(max(14, len(bars) - 24), len(bars) - 2):
        bar = bars[sweep_index]
        known = [p for p in swings if p.index + 2 < sweep_index]
        highs = [p for p in known if p.kind == 'HIGH']
        lows = [p for p in known if p.kind == 'LOW']
        if variant == 'swing':
            if min(len(highs), len(lows)) < 2:
                continue
            if any((sign * (p[-1].price - p[-2].price) >= 0 for p in (highs, lows))):
                continue
            swept = (lows if long else highs)[-1]
            broken = (highs if long else lows)[-1]
            level, reference = (swept.price, swept.time_ms)
            if any((candidate_bar.low < level if long else candidate_bar.high > level for candidate_bar in bars[swept.index + 1:sweep_index])):
                continue
            if any((sign * (candidate_bar.close - broken.price) > 0 for candidate_bar in bars[broken.index + 1:sweep_index + 1])):
                continue
        else:
            ref = min(bars[sweep_index - 12:sweep_index], key=lambda candidate_bar: candidate_bar.low) if long else max(bars[sweep_index - 12:sweep_index], key=lambda candidate_bar: candidate_bar.high)
            level, reference = (ref.low if long else ref.high, ref.time_ms)
        extreme = bar.low if long else bar.high
        if sign * (level - extreme) <= 0 or sign * (bar.close - level) <= 0:
            continue
        for break_index in range(sweep_index + 1, min(sweep_index + 13, len(bars))):
            candidate_bar = bars[break_index]
            if sign * (candidate_bar.close - extreme) <= 0:
                break
            if variant == 'swing':
                price, pivot_time = (broken.price, broken.time_ms)
                if sign * (candidate_bar.close - price) <= 0:
                    continue
                if candidate_bar.high <= candidate_bar.low or sign * (candidate_bar.close - candidate_bar.open) < DISPLACEMENT_BODY_RATIO * (candidate_bar.high - candidate_bar.low):
                    break
                token = 'MSS'
            else:
                window = bars[:break_index + 1]
                token = next((name for name in ('mss', 'bos') if _token_on_bar(name, direction=direction, window=window, zone_lo=0, zone_hi=0, structured={})), None)
                if token is None:
                    continue
                n = 12 if token == 'mss' else 8
                ref = max(bars[break_index - n:break_index], key=lambda gap_index: gap_index.high) if long else min(bars[break_index - n:break_index], key=lambda gap_index: gap_index.low)
                price, pivot_time = (ref.high if long else ref.low, ref.time_ms)
                token = token.upper()
            for gap_index in range(max(break_index, sweep_index + 2), min(break_index + 2, len(bars))):
                left, right = (bars[gap_index - 2], bars[gap_index])
                zone_low, zone_high = (left.high, right.low) if long else (right.high, left.low)
                if zone_low >= zone_high:
                    continue
                key = f'{variant}:{direction}:{bar.time_ms}:{candidate_bar.time_ms}:{right.time_ms}'
                reference_index = next((n for n, x in enumerate(bars) if x.time_ms == reference))
                pivot_index = next((n for n, x in enumerate(bars) if x.time_ms == pivot_time))
                visible = bars[max(0, min(reference_index, pivot_index, sweep_index, gap_index - 2) - 2):gap_index + 1]
                found.append(dict(key=key, variant=variant, direction=direction, sweep_ms=bar.time_ms, reference_ms=reference, reference=level, extreme=extreme, break_ms=candidate_bar.time_ms, pivot_ms=pivot_time, pivot=price, token=token, fvg_ms=right.time_ms, zone=[zone_low, zone_high], origin=[bars[m].time_ms for m in (gap_index - 2, gap_index - 1, gap_index)], bars=[[x.time_ms, x.open, x.high, x.low, x.close] for x in visible]))
                break
            break
    return found

def start(evidence, bars, settings, now_ms, minutes=()):
    zone_low, zone_high = evidence['zone']
    later = [candidate_bar for candidate_bar in bars if candidate_bar.time_ms > evidence['fvg_ms']]
    long = evidence['direction'] == 'LONG'
    sign = 1 if long else -1
    entry = round((zone_low + zone_high) / 2, 1)
    stop = round(evidence['extreme'] - sign * 0.1, 1)
    track = dict(entry=entry, stop=stop, expires_ms=evidence['fvg_ms'] + STEP + HOUR, cursor=now_ms // MIN * MIN - MIN, registered_ms=now_ms, costs={name: getattr(settings, name) for name in ('default_margin_usdt', 'default_leverage', 'maker_fee_rate', 'taker_fee_rate', 'slippage_bps', 'default_tp_behavior')})
    for candidate_bar in later:
        if sign * (candidate_bar.close - stop) <= 0:
            return ('invalidated', track)
        if candidate_bar.low <= zone_high and candidate_bar.high >= zone_low:
            track['touch_ms'] = candidate_bar.time_ms
            return ('late_touch', track)
    for bar in minutes:
        if bars[-1].time_ms + STEP <= bar.time_ms and bar.time_ms + MIN <= now_ms and (bar.low <= zone_high) and (bar.high >= zone_low):
            track['touch_ms'] = bar.time_ms
            return ('late_touch', track)
    if sign * (entry - stop) <= 0:
        return ('invalidated', track)
    if now_ms >= track['expires_ms']:
        return ('expired', track)
    return ('await_touch', track)

def advance(row, five, minutes, now_ms):
    """Advance chronologically; each closed minute is consumed at most once."""
    evidence = row.evidence
    tracking = dict(row.tracking)
    state = row.state
    sign = 1 if evidence['direction'] == 'LONG' else -1
    entry, stop = (tracking['entry'], tracking['stop'])
    zone_low, zone_high = evidence['zone']
    costs = SimpleNamespace(**tracking['costs'])
    for bar in minutes:
        if bar.time_ms <= tracking['cursor'] or bar.time_ms + MIN > now_ms:
            continue
        if bar.time_ms != tracking['cursor'] + MIN or not valid_series([bar], MIN):
            state = 'data_gap'
            break
        tracking['cursor'] = bar.time_ms
        if bar.time_ms < tracking.get('registered_ms', 0):
            if bar.low <= zone_high and bar.high >= zone_low:
                tracking['touch_ms'] = bar.time_ms
                state = 'registration_ambiguous'
                break
            continue
        if state != 'filled' and bar.time_ms >= tracking['expires_ms']:
            state = 'expired'
            break
        if state == 'await_touch':
            if bar.low <= zone_high and bar.high >= zone_low:
                tracking.setdefault('touch_ms', bar.time_ms)
            if (bar.time_ms + MIN) % STEP:
                continue
            history = [candidate_bar for candidate_bar in five if candidate_bar.time_ms + STEP <= bar.time_ms + MIN][-120:]
            if len(history) < 18 or history[-1].time_ms != bar.time_ms + MIN - STEP or (not valid_series(history, STEP)):
                state = 'data_gap'
                break
            if sign * (history[-1].close - stop) <= 0:
                state = 'invalidated'
                break
            if 'touch_ms' not in tracking:
                continue
            cap = entry + sign * 2.5 * abs(entry - stop)
            if target_at_two_r(costs, evidence['direction'], entry, stop, cap) is None:
                state = 'cost_cap'
                break
            take_profit, _ = structural_target(costs, evidence['direction'], entry, stop, cap, history)
            if take_profit is None:
                state = 'obstacle'
                break
            state = 'limit'
            tracking.update(tp=take_profit, ready_ms=bar.time_ms + MIN)
            continue
        take_profit = tracking['tp']
        hit_tp = bar.high >= take_profit if sign == 1 else bar.low <= take_profit
        hit_sl = bar.low <= stop if sign == 1 else bar.high >= stop
        if state == 'limit':
            touched = bar.low <= entry if sign == 1 else bar.high >= entry
            if not touched:
                if hit_tp:
                    state = 'target_before_fill'
                    break
                continue
            tracking['fill_ms'] = bar.time_ms
            state = 'filled'
            if hit_tp and (not hit_sl) and (sign * (bar.open - entry) > 0) and (sign * (bar.close - take_profit) < 0):
                state = 'ambiguous'
                break
        if hit_tp and hit_sl:
            state = 'ambiguous'
            break
        if hit_tp or hit_sl:
            state = 'tp' if hit_tp else 'sl'
            exit_price = take_profit if hit_tp else (min(stop, bar.open) if sign == 1 else max(stop, bar.open)) * (1 - sign * costs.slippage_bps / 10000)
            exit_fee_rate = costs.maker_fee_rate if hit_tp and costs.default_tp_behavior == 'limit' else costs.taker_fee_rate
            quantity = costs.default_margin_usdt * costs.default_leverage / entry
            tracking.update(exit_ms=bar.time_ms, net_usdt=quantity * (sign * (exit_price - entry) - entry * costs.maker_fee_rate - exit_price * exit_fee_rate))
            break
    row.state, row.tracking = (state, tracking)

async def _tick():
    from app.db import SessionLocal
    from app.services.market_data import MarketDataService
    from app.services.entry_strategies import A, strategy_settings
    from app.services.entry_execution_settings import get_paper_entry_execution_config
    async with SessionLocal() as session:
        settings = await session.get(SimPositionSettings, 1)
        if not settings or not (await strategy_settings(session))[A] or (await get_paper_entry_execution_config(session))[0] != 'STRICT':
            return 'inactive_settings'
        market = MarketDataService()
        five = (await market.fetch_kline_bundle('BTCUSDT', '5m', 160)).closed
        minutes = (await market.fetch_kline_bundle('BTCUSDT', '1m', 200)).closed
        now_ms = int(time.time() * 1000)
        active = (await session.scalars(select(StructureObservation).where(StructureObservation.state.in_(ACTIVE)))).all()
        for row in active:
            advance(row, five, minutes, now_ms)
        if five and minutes and (minutes[-1].time_ms == now_ms // MIN * MIN - MIN) and valid_series(minutes[-6:], MIN) and (five[-1].time_ms == now_ms // STEP * STEP - STEP):
            for variant in ('rolling', 'swing'):
                for direction in ('LONG', 'SHORT'):
                    for evidence in detect(five, direction, variant):
                        state, tracking = start(evidence, five, settings, now_ms, minutes)
                        await session.execute(insert(StructureObservation).values(key=evidence['key'], observed_ms=now_ms, state=state, evidence=evidence, tracking=tracking).on_conflict_do_nothing(index_elements=['key']))
        await session.commit()
        if not five or not minutes or (not valid_series(five, STEP)) or (not valid_series(minutes, MIN)):
            return 'invalid_or_incomplete_candles'
        if five[-1].time_ms != now_ms // STEP * STEP - STEP or minutes[-1].time_ms != now_ms // MIN * MIN - MIN:
            return 'stale_candles'
        return 'ok'

async def tick():
    from app.db import SessionLocal
    from app.services.settings_store import set_plain
    import logging
    try:
        status = await _tick()
    except Exception:
        logging.getLogger(__name__).exception('Structural shadow observation failed')
        status = 'collection_failed'
    async with SessionLocal() as session:
        await set_plain(session, 'structure_shadow', 'status', status)
        await set_plain(session, 'structure_shadow', 'checked_ms', str(int(time.time() * 1000)))
        await session.commit()
REVIEW = '\nIndependently review each supplied shadow_candidate against the closed 1h and 4h context.\nInclude shadow_review as an array of {key, verdict: "watch" or "skip", reason_fa}.\nCopy its exact key; its referenced swings, break and zone are immutable. Do not use\nthis secondary review to change your primary plan. This review cannot place an order.\n'

async def reviewable(session):
    rows = (await session.scalars(select(StructureObservation).where(StructureObservation.state == 'await_touch', StructureObservation.key.like('swing:%'), StructureObservation.observed_ms > int(time.time() * 1000) - HOUR).order_by(StructureObservation.observed_ms.desc()).limit(8))).all()
    return [{name: value for name, value in row.evidence.items() if name != 'bars'} for row in rows]

def validate_review(candidates, raw):
    raw = raw if isinstance(raw, list) else []
    result = {}
    for candidate in candidates:
        matches = [r for r in raw if isinstance(r, dict) and r.get('key') == candidate['key']]
        verdict, reason = ('unreviewed', '')
        if len(matches) == 1 and matches[0].get('verdict') in ('watch', 'skip'):
            verdict = matches[0]['verdict']
            reason = str(matches[0].get('reason_fa') or '')[:240]
        result[candidate['key']] = {'verdict': verdict, 'reason': reason}
    return result
