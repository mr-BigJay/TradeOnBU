import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DEFAULTS = dict(capital=0.0, margin=20.0, leverage=3, fee_bps=6.0, enabled=False,
                activated_at=None, expiry_hours=0, max_symbols=20)

class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS signals(id TEXT PRIMARY KEY, symbol TEXT, status TEXT NOT NULL,
              received REAL NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS active_signals ON signals(status, symbol);
            CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY, signal_id TEXT, at REAL NOT NULL,
              kind TEXT NOT NULL, amount REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS candles(symbol TEXT, at INTEGER, data TEXT NOT NULL,
              PRIMARY KEY(symbol, at));
            CREATE TABLE IF NOT EXISTS quotes(symbol TEXT PRIMARY KEY, at REAL NOT NULL, price REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS status(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
            ''')
            db.execute('INSERT OR IGNORE INTO settings VALUES(1,?)', (json.dumps(DEFAULTS),))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def settings(db):
        return {**DEFAULTS, **json.loads(db.execute('SELECT data FROM settings WHERE id=1').fetchone()[0])}

    @staticmethod
    def save(db, s):
        db.execute('INSERT INTO signals VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET symbol=excluded.symbol,status=excluded.status,data=excluded.data',
                   (s['id'], s.get('symbol'), s['status'], s['received'], json.dumps(s, ensure_ascii=False)))

    @staticmethod
    def rows(db, active=False):
        q = "SELECT data FROM signals" + (" WHERE status IN ('waiting','open')" if active else '') + ' ORDER BY received, id'
        return [json.loads(r[0]) for r in db.execute(q)]

    @staticmethod
    def wallet(db):
        settings = Store.settings(db)
        balance = db.execute('SELECT COALESCE(SUM(amount),0) FROM ledger').fetchone()[0]
        reserved = sum(s.get('margin', 0) * s.get('remaining', 0) for s in Store.rows(db, True) if s['status'] == 'open')
        unrealized = 0.0
        for s in Store.rows(db, True):
            if s['status'] == 'open':
                q = db.execute('SELECT price FROM quotes WHERE symbol=?', (s['symbol'],)).fetchone()
                if q:
                    unrealized += (q[0]-s['entry']) * s['quantity'] * s['remaining'] * (1 if s['direction']=='LONG' else -1)
        return dict(**settings, balance=balance, reserved=reserved, available=balance-reserved,
                    unrealized=unrealized, equity=balance+unrealized)
