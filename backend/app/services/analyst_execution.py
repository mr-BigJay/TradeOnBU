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
    finish=max(t for name,t in stamps.items() if name!='entry_zone_touch')
    span=[b for b in bars if stamp<=b.time_ms<=finish]
    expected=list(range(stamp,finish+300000,300000))
    if [b.time_ms for b in span]!=expected or finish+300000>now_ms:
        return None, 'execution_structure_evidence_missing'
    long = setup.direction == 'LONG'
    # A later confirmation candle can sweep deeper than the first sweep.
    # Protect the entire observed pattern, not just its opening event.
    extreme = min(b.low for b in span) if long else max(b.high for b in span)
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
    return dict(version='structure_bound_v2', entry=contract['entry'], stop=stop, target=target,
                proposal_stop=contract['stop'], proposal_target=contract['target'],
                confirmation_token=token, confirmation_ms=stamp, confirmation_end_ms=finish, extreme=extreme,
                obstacle=obstacle, assessed_at_ms=now_ms), None
