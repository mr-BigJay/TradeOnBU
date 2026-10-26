import asyncio
import csv
import io
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from .store import Store
from .engine import ingest, apply_bars, apply_quote
from .market import Market

class Config(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    margin: float=Field(gt=0,le=1_000_000)
    leverage: int=Field(ge=1,le=125)
    fee_bps: float=Field(ge=0,le=100)
    expiry_hours: int=Field(ge=0,le=720)
    enabled: bool

class Capital(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    amount: float=Field(gt=0,le=100_000_000)

class Runner:
    def __init__(self,store,inbox,market=None):
        self.store=store; self.inbox=Path(inbox); self.market=market or Market(); self.last_bars={}
    async def tick(self):
        now=time.time(); fresh=False
        try:
            feed=json.loads((self.inbox/'_feed.json').read_text())
            fresh=0<=now-float(feed['heartbeat'])<60
        except (OSError,ValueError,KeyError,TypeError): pass
        # Persist incoming events independently of the reader's bounded inbox.
        if fresh:
            for path in sorted(self.inbox.glob('*.json')):
                if path.name.startswith('_'): continue
                try:
                    if path.stat().st_size>32768: continue
                    event=json.loads(path.read_text())
                    if isinstance(event,dict):
                        with self.store.connect() as db: ingest(db,event,now)
                except (OSError,ValueError,KeyError,TypeError): continue
        with self.store.connect() as db:
            cfg=Store.settings(db); active=Store.rows(db,True)
            for s in active:
                if s['status']=='waiting' and cfg['expiry_hours'] and now-s['received']>cfg['expiry_hours']*3600:
                    s.update(status='expired',reason='configured_expiry'); Store.save(db,s)
            active=Store.rows(db,True)
            # Open positions always take priority; disable only blocks new entries.
            ordered=sorted(active,key=lambda s:(s['status']!='open',s['received']))
            symbols=list(dict.fromkeys(s['symbol'] for s in ordered))
            selected=symbols[:cfg['max_symbols']]
            for s in active:
                if s['symbol'] not in selected and s['status']=='waiting':
                    s['reason']='capacity_queue'; s['uncertain']=True; Store.save(db,s)
        errors={}
        # Do not halt exit monitoring merely because Telegram is disconnected.
        due=sorted((s for s in selected if now-self.last_bars.get(s,0)>=60),key=lambda s:self.last_bars.get(s,0))[:2]
        for symbol in due:
            try:
                bars=await self.market.bars(symbol)
                with self.store.connect() as db: apply_bars(db,symbol,bars)
                self.last_bars[symbol]=now
            except Exception: errors[symbol]='candles_unavailable'
            await asyncio.sleep(0.15)  # Bounded public-API traffic, also on repeated failures.
        if selected:
            try:
                prices=await self.market.quotes(selected)
                with self.store.connect() as db:
                    for symbol,price in prices.items(): apply_quote(db,symbol,price,time.time())
                    for symbol in set(selected)-prices.keys(): errors[symbol]='symbol_unavailable'
            except Exception:
                for symbol in selected: errors[symbol]='quotes_unavailable'
        with self.store.connect() as db:
            status=dict(heartbeat=time.time(),reader_fresh=fresh,active_symbols=selected,
                        queued_symbols=max(0,len(symbols)-len(selected)),errors=errors,paper_only=True)
            db.execute('INSERT OR REPLACE INTO status VALUES(1,?)',(json.dumps(status),))
            # Keep all raw candles for ongoing signals and 30 days after activity;
            # permanent signal/ledger records are never pruned by this job.
            db.execute("DELETE FROM candles WHERE at<? AND symbol NOT IN (SELECT symbol FROM signals WHERE status IN ('waiting','open'))",(now-30*86400,))
    async def run(self):
        while True:
            try: await self.tick()
            except asyncio.CancelledError: raise
            except Exception:
                with self.store.connect() as db:
                    db.execute('INSERT OR REPLACE INTO status VALUES(1,?)',(json.dumps(dict(heartbeat=time.time(),error='worker_error',paper_only=True)),))
            await asyncio.sleep(5)


def report(rows):
    closed=sorted([s for s in rows if s['status']=='closed'],key=lambda s:s['closed_at'])
    clean=[s for s in closed if not s['uncertain']]
    wins=sum(s['pnl']>1e-8 for s in clean); losses=sum(s['pnl']<-1e-8 for s in clean)
    realized=peak=drawdown=0
    for s in closed:
        realized+=s['pnl']; peak=max(peak,realized); drawdown=max(drawdown,peak-realized)
    by_symbol={}
    for s in clean:
        v=by_symbol.setdefault(s['symbol'],dict(closed=0,wins=0,net=0))
        v['closed']+=1; v['wins']+=s['pnl']>1e-8; v['net']+=s['pnl']
    return dict(total=len(rows),closed=len(closed),certain_closed=len(clean),wins=wins,losses=losses,
                breakeven=len(clean)-wins-losses,winrate=100*wins/len(clean) if clean else None,
                uncertain_closed=len(closed)-len(clean),net=sum(s['pnl'] for s in closed),
                fees=sum(s.get('fees',0) for s in rows),realized_drawdown=drawdown,by_symbol=by_symbol)

def create_app(path=None,background=True):
    store=Store(path or os.getenv('BEHDARK_DB','/behdark-data/behdark.db'))
    @asynccontextmanager
    async def lifespan(app):
        runner=Runner(store,os.getenv('BEHDARK_INBOX','/telegram-inbox')) if background else None
        task=asyncio.create_task(runner.run()) if runner else None
        yield
        if task:
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass
            await runner.market.close()
    api=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    api.state.store=store
    @api.get('/health')
    def health():
        with store.connect() as db:
            s=db.execute('SELECT data FROM status WHERE id=1').fetchone()
        data=json.loads(s[0]) if s else {}
        return dict(status='ok',paper_only=True,worker_fresh=0<=time.time()-data.get('heartbeat',0)<120 and not data.get('error'))
    @api.get('/snapshot')
    def snapshot():
        with store.connect() as db:
            rows=Store.rows(db); wallet=Store.wallet(db)
            state=db.execute('SELECT data FROM status WHERE id=1').fetchone()
            quotes={r['symbol']:dict(price=r['price'],at=r['at']) for r in db.execute('SELECT * FROM quotes')}
        active=[s for s in rows if s['status'] in ('waiting','open')]
        history=[s for s in rows if s['status'] not in ('waiting','open')][-200:]
        return dict(wallet=wallet,signals=list(reversed(active+history)),report=report(rows),quotes=quotes,
                    status=json.loads(state[0]) if state else dict(error='starting'),history_limit=200)
    @api.put('/settings')
    def settings(body:Config):
        with store.connect() as db:
            cfg=Store.settings(db)
            if body.enabled and cfg['capital']<=0: raise HTTPException(409,'ابتدا موجودی آزمایشی را ثبت کنید.')
            cfg.update(body.model_dump())
            if body.enabled and cfg['activated_at'] is None: cfg['activated_at']=time.time()
            db.execute('UPDATE settings SET data=? WHERE id=1',(json.dumps(cfg),))
        return {'ok':True}
    @api.post('/capital')
    def capital(body:Capital):
        with store.connect() as db:
            cfg=Store.settings(db)
            if cfg['capital']!=0: raise HTTPException(409,'موجودی اولیه قبلاً ثبت شده است؛ تاریخچه بازنشانی نمی‌شود.')
            cfg['capital']=body.amount
            db.execute('UPDATE settings SET data=? WHERE id=1',(json.dumps(cfg),))
            db.execute('INSERT INTO ledger(at,kind,amount) VALUES(?,?,?)',(time.time(),'initial_capital',body.amount))
        return {'ok':True}
    @api.post('/cancel/{mid}')
    def cancel(mid:str):
        if not mid.isdigit(): raise HTTPException(404)
        with store.connect() as db:
            row=db.execute('SELECT data FROM signals WHERE id=?',('-1878414449:'+mid,)).fetchone()
            if not row: raise HTTPException(404)
            s=json.loads(row[0])
            if s['status'] not in ('waiting','review'): raise HTTPException(409,'فقط سیگنال منتظر یا نیازمند بررسی قابل لغو است.')
            s.update(status='cancelled',reason='manual_cancel',cancelled_at=time.time()); Store.save(db,s)
        return {'ok':True}
    @api.get('/report.csv',response_class=PlainTextResponse)
    def export():
        with store.connect() as db: rows=Store.rows(db)
        out=io.StringIO(); keys=['id','symbol','direction','status','received','opened_at','closed_at','entry','stop','targets','pnl','fees','uncertain','reason']
        writer=csv.writer(out); writer.writerow(keys)
        for s in rows: writer.writerow([s.get(k,'') for k in keys])
        return PlainTextResponse('\ufeff'+out.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="behdark.csv"'})
    return api
