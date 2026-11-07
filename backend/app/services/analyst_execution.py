"""Execution geometry derived from observed confirmations, not model assertions."""
import math

from app.services.multi_setup_analyst import VERSION
from app.services.verified_trade_plan import atr, _tick
from app.services.ict_execution_plan import target_at_net_rr


def priority_key(setup):
    rank = (setup.structured_snapshot or {}).get('analyst_rank')
    rank = rank if isinstance(rank, int) and not isinstance(rank, bool) and 1 <= rank <= 3 else 99
    return rank, -(setup.run_id or 0), setup.setup_id


def execution_plan(setup, settings, bars, now_ms):
    """Keep the signed proposal immutable; derive and audit final order prices.

    Widening a stop never increases the monetary risk budget. A wider stop may
    require a farther target, but never one beyond the proposal's obstacle.
    """
    contract = (setup.structured_snapshot or {}).get('verified_trade_plan') or {}
    if contract.get('version') != VERSION:
        return None, None
    stamps = (setup.confirmation_state or {}).get('confirmation_timestamps') or {}
    token = {'ict_reversal': 'liquidity_sweep', 'trend_continuation': 'trend_pullback',
             'breakout_retest': 'breakout_retest'}[contract['strategy_id']]
    stamp = stamps.get(token)
    if not isinstance(stamp, int) or stamp + 300000 > now_ms:
        return None, 'execution_structure_unconfirmed'
    proof = ((setup.confirmation_state or {}).get('confirmation_evidence') or {}).get(token) or {}
    candle = next((b for b in bars if b.time_ms == stamp), None)
    long = setup.direction == 'LONG'
    extreme = (candle.low if long else candle.high) if candle else proof.get('bar_low' if long else 'bar_high')
    if not isinstance(extreme, (int, float)) or not math.isfinite(extreme) or extreme <= 0:
        return None, 'execution_structure_evidence_missing'
    sign = 1 if long else -1
    vol = max(atr(bars), float(contract['evidence']['atr14']))
    structural_stop = _tick(extreme - sign * max(.25 * vol, .1), sign)
    stop = min(contract['stop'], structural_stop) if long else max(contract['stop'], structural_stop)
    limit=contract['evidence'].get('stop_distance_limit')
    if limit is not None and sign*(contract['entry']-stop)>limit:
        return None, 'execution_stop_too_wide'
    obstacle = contract['evidence']['obstacle']
    target = target_at_net_rr(settings, setup.direction, contract['entry'], stop, obstacle)
    if target is None:
        return None, 'execution_insufficient_reward_before_obstacle'
    # A completed move is not an opportunity to place a late resting order.
    if sign * (bars[-1].close - target) >= 0:
        return None, 'execution_target_already_reached'
    return dict(entry=contract['entry'], stop=stop, target=target,
                proposal_stop=contract['stop'], proposal_target=contract['target'],
                confirmation_token=token, confirmation_ms=stamp, extreme=extreme,
                obstacle=obstacle, assessed_at_ms=now_ms), None
