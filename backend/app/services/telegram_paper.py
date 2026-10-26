"""Deterministic BehDark text signals -> existing simulated positions only."""
from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path

from sqlalchemy import select, text
from app.db import SessionLocal
from app.models import TelegramPaperEvent, SimPosition
from app.services.market_data import MarketDataService
from app.services.position.position_service import SimPositionService, PositionServiceError
from app.services.position.execution_adapters import OrderRequest
from app.services.position.settings_service import get_or_create_settings, fee_profile_from_settings
from app.services.runtime_settings import get_runtime_settings

PEER = '-1878414449'
NUMBER = r'\d+(?:\.\d+)?'


def parse_signal(body: str) -> dict:
    """Exact supported template only; do not interpret prose, replies or images."""
    body = body.replace('\u200e', '').replace('\u200f', '').replace('–', '-').replace('—', '-')
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if len(lines) != 6:
        raise ValueError('unsupported_template')
    head = re.fullmatch(r'[🟢🔴\s]*(Buy/Long|Sell/Short) Signal\s+#([A-Z0-9]{2,24}USDT)', lines[0], re.I)
    entry = re.fullmatch(rf'Entry range:\s*({NUMBER})\s*[-_]\s*({NUMBER})', lines[1], re.I)
    targets = re.fullmatch(rf'Targets:\s*({NUMBER})(?:\s*[-_]\s*({NUMBER}))?', lines[2], re.I)
    stop = re.fullmatch(rf'Stop:\s*({NUMBER})', lines[3], re.I)
    leverage = re.fullmatch(r'Leverage:\s*(\d+)X(?:\s*[-_]\s*(\d+)X)?', lines[4], re.I)
    balance = re.fullmatch(rf'Balance:\s*({NUMBER})% of capital', lines[5], re.I)
    if not all((head, entry, targets, stop, leverage, balance)):
        raise ValueError('unsupported_template')
    low, high = sorted(map(float, entry.groups()))
    sl = float(stop[1]); tp = float(targets[1]); tp2 = float(targets[2] or targets[1])
    direction = 'LONG' if head[1].lower() == 'buy/long' else 'SHORT'
    values = [low, high, sl, tp, tp2, float(balance[1])]
    if not all(math.isfinite(x) and x > 0 for x in values):
        raise ValueError('invalid_numbers')
    if not (sl < low <= high < tp <= tp2 if direction == 'LONG' else tp2 <= tp < low <= high < sl):
        raise ValueError('invalid_levels')
    lev = min(int(leverage[1]), int(leverage[2] or leverage[1]))
    if not 1 <= lev <= 125 or float(balance[1]) > 100:
        raise ValueError('invalid_sizing')
    return dict(symbol=head[2].upper(), direction=direction, low=low, high=high,
                stop_loss=sl, take_profit=tp, targets=[tp,tp2], leverage=lev,
                balance_pct=float(balance[1]))


def net_rr(entry, stop, target, fee):
    # Both entry and adverse exits have fees; fees increase loss and reduce reward.
    risk = abs(entry - stop) + (entry + stop) * fee
    reward = abs(target - entry) - (entry + target) * fee
    return reward / risk if risk > 0 else 0


class RangeMarket(MarketDataService):
    def __init__(self, signal):
        super().__init__()
        self.signal = signal

    async def fetch_ticker(self, symbol):
        price, ts = await super().fetch_ticker(symbol)
        s = self.signal
        if not min(s['stop_loss'], s['take_profit']) < price < max(s['stop_loss'], s['take_profit']):
            raise ValueError('signal_already_invalidated')
        if not s['low'] <= price <= s['high']:
            raise ValueError('outside_entry_range')
        return price, ts


async def process_event(session, event: dict, now: float | None = None):
    now = time.time() if now is None else now
    key = event.get('key', '')
    if not re.fullmatch(r'-1878414449:\d+', key) or event.get('peer') != PEER:
        return
    row = await session.get(TelegramPaperEvent, key)
    if row and row.status != 'waiting':
        return
    if not row:
        row = TelegramPaperEvent(event_key=key, status='waiting', reason='', position_id=None)
        session.add(row)
        await session.flush()
    def reject(reason):
        row.status = 'skipped'; row.reason = reason
    try:
        published = float(event['published'])
        received = float(event['received'])
        if not all(math.isfinite(x) for x in (published, received)):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        reject('invalid_timestamp'); return
    if event.get('kind') != 'new':
        reject('edited_or_baseline'); return
    if not (0 <= received - published <= 120 and 0 <= now - received <= 900):
        reject('stale_message'); return
    try:
        signal = parse_signal(event['body'])
    except (ValueError, KeyError, TypeError):
        reject('unsupported_or_invalid_signal'); return
    row.symbol = signal['symbol']
    runtime = await get_runtime_settings(session)
    if runtime.paper_trading_paused:
        reject('paper_trading_paused'); return
    active = list((await session.scalars(select(SimPosition).where(SimPosition.status.in_(['open','pending_entry'])))).all())
    if len(active) >= runtime.max_simultaneous_positions:
        row.reason = 'position_limit'; return
    if any(p.symbol == signal['symbol'] for p in active):
        row.reason = 'symbol_already_active'; return
    settings = await get_or_create_settings(session)
    from app.services.position.wallet_service import wallet_summary
    summary = await wallet_summary(session)
    # Channel capital allocation is margin, NOT loss-at-stop percentage.
    margin = min(settings.default_margin_usdt, settings.max_margin_per_position,
                 summary['wallet_balance'] * signal['balance_pct'] / 100,
                 summary['available_balance'])
    if margin <= 0:
        reject('insufficient_balance'); return
    leverage = min(settings.default_leverage, signal['leverage'])
    market = RangeMarket(signal)
    try:
        price, _ = await market.fetch_ticker(signal['symbol'])
        # Existing slippage applies to paper execution; do not fill outside the advertised range.
        from app.services.position.calculations import apply_slippage
        fill = apply_slippage(price, signal['direction'], side='entry', adverse=True, bps=settings.slippage_bps)
        if not signal['low'] <= fill <= signal['high']:
            row.reason = 'slippage_outside_range'; return
        fee = fee_profile_from_settings(settings).taker_rate
        rr = net_rr(fill, signal['stop_loss'], signal['take_profit'], fee)
        if rr < max(2.0, runtime.min_net_rr):
            reject('rr_below_minimum'); return
        svc = SimPositionService(market)
        # One savepoint contains position + reservation + fill. A changed quote cannot
        # leave a partial order behind. Event and order are committed together by caller.
        async with session.begin_nested():
            result = await svc.create_order(session, OrderRequest(
                symbol=signal['symbol'], direction=signal['direction'], margin_usdt=margin,
                leverage=leverage, order_type='market', entry_price=None,
                stop_loss=signal['stop_loss'], take_profit=signal['take_profit'],
                order_extra={'source':'behdark', 'source_label':'BehDark', 'telegram_event_key':key,
                             'channel_targets':signal['targets'], 'entry_range':[signal['low'],signal['high']]}), commit=False)
            if not result.success:
                raise ValueError('fill_rejected')
            pos = await svc.get_position_by_public_id(session, result.position_id)
            actual = pos.fill_price or pos.entry_price
            if not actual or not signal['low'] <= actual <= signal['high']:
                raise ValueError('slippage_outside_range')
            if net_rr(actual, pos.stop_loss, pos.take_profit, fee) < max(2.0, runtime.min_net_rr):
                raise ValueError('rr_below_minimum')
        row.status='opened'; row.reason=''; row.position_id=result.position_id
    except PositionServiceError:
        reject('wallet_or_risk_rejected')
    except (ValueError, RuntimeError) as exc:
        reason = str(exc)
        if reason == 'signal_already_invalidated':
            reject(reason); return
        row.reason = reason if reason in ('outside_entry_range','slippage_outside_range','rr_below_minimum','fill_rejected') else 'market_unavailable'
    except Exception:
        # No message bodies, profile values or network exception payloads in diagnostics.
        row.reason='market_unavailable'


async def telegram_paper_tick():
    if os.environ.get('TELEGRAM_PAPER_ENABLED') != '1':
        return
    directory = Path(os.environ.get('TELEGRAM_PAPER_INBOX', '/telegram-inbox'))
    try:
        feed = json.loads((directory / '_feed.json').read_text())
        if not 0 <= time.time() - float(feed['heartbeat']) <= 45:
            return
    except (OSError, ValueError, KeyError, TypeError):
        return
    # Immutable event IDs plus database primary key survive reader/app restarts.
    for path in sorted(directory.glob('*.json'))[-500:]:
        try:
            if path.stat().st_size > 32768:
                continue
            event = json.loads(path.read_text())
            if not isinstance(event, dict):
                continue
            async with SessionLocal() as session:
                # Production uses SQLite. Serialize read-check-create against other writers.
                await session.execute(text('BEGIN IMMEDIATE'))
                async with session.begin_nested():
                    await process_event(session, event)
                await session.commit()
        except (OSError, ValueError):
            continue
