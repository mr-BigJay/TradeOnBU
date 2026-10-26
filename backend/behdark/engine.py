"""Deterministic independent paper ledger. No exchange orders or AI calls."""
import hashlib
import json
import math
import re
import time
from .store import Store

PEER = '-1878414449'
NUMBER = r'\d+(?:\.\d+)?'

def parse(body):
    body = body.replace('\u200e','').replace('\u200f','').replace('–','-').replace('—','-')
    lines = [x.strip() for x in body.splitlines() if x.strip()]
    if len(lines) != 6: raise ValueError('unsupported_template')
    head = re.fullmatch(r'[🟢🔴\s]*(Buy/Long|Sell/Short) Signal\s+#([A-Z0-9]{2,24}USDT)', lines[0], re.I)
    entry = re.fullmatch(rf'Entry range:\s*({NUMBER})\s*[-_]\s*({NUMBER})',lines[1],re.I)
    targets = re.fullmatch(rf'Targets:\s*({NUMBER}(?:\s*[-_]\s*{NUMBER})*)',lines[2],re.I)
    stop = re.fullmatch(rf'Stop:\s*({NUMBER})',lines[3],re.I)
    lev = re.fullmatch(r'Leverage:\s*(\d+)X(?:\s*[-_]\s*(\d+)X)?',lines[4],re.I)
    allocation = re.fullmatch(rf'Balance:\s*({NUMBER})% of capital',lines[5],re.I)
    if not all((head,entry,targets,stop,lev,allocation)): raise ValueError('unsupported_template')
    low,high = sorted(map(float,entry.groups())); sl=float(stop[1])
    tps = list(dict.fromkeys(float(x.strip()) for x in re.split('[-_]',targets[1])))
    direction = 'LONG' if head[1].lower()=='buy/long' else 'SHORT'
    if not 1 <= len(tps) <= 10 or not all(math.isfinite(x) and x>0 for x in [low,high,sl,*tps]):
        raise ValueError('invalid_levels')
    if direction=='LONG':
        valid=sl<low<=high<min(tps) and tps==sorted(tps)
    else:
        valid=max(tps)<low<=high<sl and tps==sorted(tps,reverse=True)
    if not valid: raise ValueError('invalid_levels')
    if not 1<=int(lev[1])<=125 or not 0<float(allocation[1])<=100:
        raise ValueError('invalid_channel_sizing')
    return dict(symbol=head[2].upper(),direction=direction,low=low,high=high,stop=sl,targets=tps,
                channel_leverage=int(lev[1]),channel_allocation=float(allocation[1]))

def ingest(db, event, now):
    key=event.get('key','')
    if event.get('peer')!=PEER or not re.fullmatch(r'-1878414449:\d+',key): return
    body=event.get('body','')
    if not isinstance(body,str) or len(body)>20000: return
    digest=hashlib.sha256(body.encode()).hexdigest()
    old=db.execute('SELECT data FROM signals WHERE id=?',(key,)).fetchone()
    if old:
        s=json.loads(old[0])
        if s['digest']!=digest:
            # Preserve original levels and full revision audit; never silently alter a filled trade.
            s.setdefault('revisions',[]).append(dict(at=now,body=body))
            s['digest']=digest; s['reason']='message_edited'
            s['uncertain']=True
            if s['status']=='waiting': s['status']='review'
            Store.save(db,s)
        return
    config=Store.settings(db)
    if config['activated_at'] is None: return
    try:
        received=float(event['received']); published=float(event['published'])
        if not all(math.isfinite(x) for x in (received,published)): return
    except (KeyError,ValueError,TypeError): return
    # Activation cutoff is persistent; pre-activation history never becomes an order.
    if published < config['activated_at'] or received > now+5: return
    s=dict(id=key,received=received,published=published,body=body,digest=digest,status='waiting',
           reason='',uncertain=False,remaining=0,pnl=0.0,fees=0.0,target_hits=[],revisions=[])
    try: s.update(parse(body))
    except (ValueError,TypeError): s.update(status='review',reason='unsupported_template')
    if event.get('kind')!='new' or event.get('has_reply'):
        s.update(status='review',reason='reply_or_edited_message')
    if not 0 <= received-published <= 120 or now-received>120:
        s.update(status='review',reason='late_receipt')
    Store.save(db,s)

def ledger(db,s,kind,amount,at):
    db.execute('INSERT INTO ledger(signal_id,at,kind,amount) VALUES(?,?,?,?)',(s['id'],at,kind,amount))

def open_trade(db,s,price,at):
    cfg=Store.settings(db)
    if not cfg['enabled']: s['reason']='paused'; return
    w=Store.wallet(db)
    margin=cfg['margin']; qty=margin*cfg['leverage']/price; fee=qty*price*cfg['fee_bps']/10000
    if w['available']+1e-8 < margin+fee:
        s['reason']='insufficient_balance'; return
    # Freeze every execution setting at entry. Configuration changes affect only new trades.
    s.update(status='open',entry=price,opened_at=at,margin=margin,leverage=cfg['leverage'],quantity=qty,
             remaining=1.0,fee_bps=cfg['fee_bps'],pnl=-fee,fees=fee,reason='',exit_fills=[])
    ledger(db,s,'entry_fee',-fee,at)

def close_part(db,s,price,fraction,at,reason):
    fraction=min(s['remaining'],fraction)
    if fraction<=1e-10: return
    sign=1 if s['direction']=='LONG' else -1
    qty=s['quantity']*fraction
    gross=(price-s['entry'])*qty*sign; fee=qty*price*s['fee_bps']/10000
    # Isolated paper margin: loss cannot consume more than the released margin share.
    net=max(gross-fee,-s['margin']*fraction)
    s['pnl']+=net; s['fees']+=fee; s['remaining']=max(0.0,s['remaining']-fraction)
    s['exit_fills'].append(dict(price=price,fraction=fraction,at=at,reason=reason,net=net))
    ledger(db,s,reason,net,at)
    if s['remaining']<1e-9:
        s.update(status='closed',remaining=0.0,closed_at=at,close_reason=reason)

def advance(db,s,low,high,price,start,end,is_bar=False):
    """Closed bars recover missed touches; ambiguous chronology is explicitly marked."""
    if s['status'] not in ('waiting','open'): return
    cfg=Store.settings(db)
    if s['status']=='waiting':
        if cfg['expiry_hours'] and end-s['received']>cfg['expiry_hours']*3600:
            s.update(status='expired',reason='configured_expiry'); return
        if start<s['received']: return
        if not cfg['enabled']: s['reason']='paused'; return
        stop_hit=low<=s['stop'] if s['direction']=='LONG' else high>=s['stop']
        touched=low<=s['high'] and high>=s['low']
        target_hit=high>=s['targets'][0] if s['direction']=='LONG' else low<=s['targets'][0]
        if is_bar and touched and (stop_hit or target_hit):
            s.update(status='review',reason='ambiguous_entry_candle',uncertain=True); return
        if stop_hit:
            s.update(status='invalidated',reason='stop_before_entry'); return
        if touched:
            fill=(s['high'] if s['direction']=='LONG' else s['low']) if is_bar else price
            if s['low']<=fill<=s['high']:
                open_trade(db,s,fill,end)
        return
    if start<s['opened_at']: return
    sign=1 if s['direction']=='LONG' else -1
    stop_hit=low<=s['stop'] if sign==1 else high>=s['stop']
    liquidation=s['entry']*(1-sign/s['leverage'])
    liq_hit=(low<=liquidation if sign==1 else high>=liquidation)
    hits=[i for i,t in enumerate(s['targets']) if i not in s['target_hits'] and (high>=t if sign==1 else low<=t)]
    if stop_hit or liq_hit:
        if is_bar and hits: s['uncertain']=True
        # Use the closer protective threshold; margin exhaustion is approximate,
        # not an exchange liquidation model (no maintenance margin or funding).
        threshold=max(s['stop'],liquidation) if sign==1 else min(s['stop'],liquidation)
        exit_price=(min(price,threshold) if sign==1 else max(price,threshold))
        reason='margin_exhausted' if threshold==liquidation else 'stop'
        close_part(db,s,exit_price,s['remaining'],end,reason)
        return
    for i in hits:
        s['target_hits'].append(i)
        close_part(db,s,s['targets'][i],1/len(s['targets']),end,f'tp{i+1}')


def apply_quote(db,symbol,price,at):
    db.execute('INSERT OR REPLACE INTO quotes VALUES(?,?,?)',(symbol,at,price))
    for s in Store.rows(db,True):
        if s['symbol']!=symbol: continue
        if s.get('last_quote') and at-s['last_quote']>30: s['uncertain']=True
        if at-s['received']>30 and not s.get('last_quote') and s['status']=='waiting': s['uncertain']=True
        s['last_quote']=at
        advance(db,s,price,price,price,at,at)
        Store.save(db,s)

def apply_bars(db,symbol,bars):
    for bar in sorted(bars,key=lambda b:b['at']):
        db.execute('INSERT OR IGNORE INTO candles VALUES(?,?,?)',(symbol,bar['at'],json.dumps(bar)))
        for s in Store.rows(db,True):
            if s['symbol']!=symbol or bar['at']<=s.get('bar_cursor',0): continue
            s['bar_cursor']=bar['at']
            # Recovery from OHLC cannot establish exact intra-minute chronology.
            before=(s['status'],s.get('remaining',0))
            advance(db,s,bar['low'],bar['high'],bar['open'],bar['at'],bar['at']+60,True)
            if (before[0]=='waiting' and s['status']=='open') or ((s['status'],s.get('remaining',0)) != before and bar['at']+60 < s.get('last_quote',0)):
                s['uncertain']=True
            Store.save(db,s)
