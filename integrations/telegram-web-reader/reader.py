"""Isolated Telegram Web K visibility probe. Never submits orders or logs bodies."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

STATE = Path(os.environ.get('READER_STATE', '/state'))
PEER = os.environ.get('CHANNEL_PEER', '-1878414449')
TITLE = os.environ.get('CHANNEL_TITLE', 'BehDark (VIP)')

# Read rendered DOM only; do not access Telegram stores, network APIs or media.
EXTRACT = r"""({peer, title}) => {
 const visible = e => !!(e && e.getClientRects().length);
 const headers = [...document.querySelectorAll('.chat-info .user-title')].filter(visible);
 if (headers.length !== 1 || headers[0].innerText.trim() !== title)
   return {state:'waiting_for_channel', messages:[]};
 if (location.hash.split('_')[0] !== '#' + peer)
   return {state:'wrong_channel', messages:[]};
 // Web K also assigns bubbles-inner to its outgoing-message animation buffer.
 // Exclude that buffer, but keep refusing ambiguous active message containers.
 const allRoots = [...document.querySelectorAll('.bubbles-inner')].filter(visible);
 const roots = allRoots.filter(e => !e.closest('.bubbles-remover, .bubbles-remover-container'));
 if (roots.length !== 1) return {state:'selector_mismatch', messages:[],
   diagnostics:{visible_roots:allRoots.length, active_roots:roots.length}};
 const bubbles = [...roots[0].querySelectorAll('.bubble[data-mid][data-peer-id]')]
   .filter(e => !e.classList.contains('service') && !e.classList.contains('is-date'));
 if (bubbles.some(e => e.dataset.peerId !== peer))
   return {state:'peer_mismatch', messages:[]};
 const messages = bubbles.map(e => {
   const body = e.querySelector('.message');
   const clone = body?.cloneNode(true);
   clone?.querySelectorAll('.time, .reply, .replies, .peer-title').forEach(e=>e.remove());
   clone?.querySelectorAll('br').forEach(e=>e.replaceWith('\n'));
   return {id:e.dataset.mid, timestamp:e.dataset.timestamp || '', has_reply:!!e.querySelector('.reply'),
           text:clone?.textContent?.trim() || ''};
 });
 // Follow the bottom of this channel only. Next poll sees newly rendered bubbles.
 const scroll = roots[0].closest('.scrollable');
 if (scroll && scroll.scrollHeight - scroll.clientHeight - scroll.scrollTop > 2)
   scroll.scrollTop = scroll.scrollHeight;
 return {state: messages.length ? 'observing' : 'no_readable_messages', messages};
}"""


def connect(path):
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE IF NOT EXISTS messages(peer TEXT, mid TEXT, body TEXT, digest TEXT, seen REAL, kind TEXT, PRIMARY KEY(peer,mid))')
    db.execute('CREATE TABLE IF NOT EXISTS exports(event_key TEXT PRIMARY KEY, payload TEXT, received REAL)')
    return db


def record(db, messages, baseline=False, now=None, export=False):
    now = time.time() if now is None else now
    counts = {'baseline': 0, 'new': 0, 'edited': 0, 'unsupported': 0}
    with db:
        for msg in messages:
            mid, body = str(msg.get('id', '')), msg.get('text', '')
            if not mid.isdigit() or not isinstance(body, str):
                continue
            body = body[:20000]
            digest = hashlib.sha256(body.encode()).hexdigest()
            old = db.execute('SELECT digest FROM messages WHERE peer=? AND mid=?', (PEER, mid)).fetchone()
            if old and old[0] == digest:
                continue
            kind = 'baseline' if baseline else ('edited' if old else 'new')
            if not body:
                kind = 'unsupported'
            counts[kind] += 1
            if export:
                key = PEER + ':' + mid
                prior_export = db.execute('SELECT payload FROM exports WHERE event_key=?', (key,)).fetchone()
                try:
                    published = float(msg.get('timestamp', 0))
                except (TypeError, ValueError):
                    published = 0
                # Publish only newly seen fresh posts; existing history is never executable.
                if kind == 'new' and not msg.get('has_reply') and 0 <= now - published <= 120:
                    payload = dict(key=key, peer=PEER, body=body, published=published, received=now, kind='new')
                    db.execute('INSERT OR IGNORE INTO exports VALUES(?,?,?)', (key,json.dumps(payload),now))
                elif prior_export:
                    payload = json.loads(prior_export[0])
                    payload.update(body=body, kind='edited')
                    db.execute('UPDATE exports SET payload=? WHERE event_key=?', (json.dumps(payload),key))
            db.execute('INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?,?)', (PEER, mid, body, digest, now, kind))
        db.execute('DELETE FROM exports WHERE received < ?', (now - 86400,))
        db.execute('DELETE FROM messages WHERE seen < ?', (now - 7 * 86400,))
        db.execute('DELETE FROM messages WHERE rowid NOT IN (SELECT rowid FROM messages ORDER BY seen DESC LIMIT 500)')
    return counts


def flush_exports(db, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    keys = set()
    for key, payload in db.execute('SELECT event_key, payload FROM exports'):
        name = hashlib.sha256(key.encode()).hexdigest() + '.json'
        keys.add(name)
        destination = directory / name
        if destination.exists() and destination.read_text() == payload:
            continue
        temp = directory / (name + '.tmp')
        temp.write_text(payload)
        temp.replace(destination)
    for old in directory.glob('*.json'):
        if old.name not in keys and old.name != '_feed.json':
            old.unlink()
    temp = directory / '_feed.tmp'
    temp.write_text(json.dumps({'heartbeat':time.time()}))
    temp.replace(directory / '_feed.json')


def status(state, **extra):
    payload = dict(state=state, heartbeat=time.time(), mode='read_only', headless=os.environ.get('READER_HEADLESS', '1') == '1', coverage='rendered_messages_only', **extra)
    temp = STATE / 'status.tmp'
    temp.write_text(json.dumps(payload))
    temp.replace(STATE / 'status.json')


def run():
    from playwright.sync_api import sync_playwright
    os.umask(0o077)
    STATE.mkdir(parents=True, exist_ok=True)
    db = connect(STATE / 'messages.sqlite3')
    status('starting')
    with sync_playwright() as p:
        headless = os.environ.get('READER_HEADLESS', '1') == '1'
        context = p.chromium.launch_persistent_context(str(STATE / 'profile'), headless=headless,
            executable_path='/usr/bin/chromium', viewport={'width':1280,'height':800},
            reduced_motion='reduce',
            args=['--disable-dev-shm-usage', '--disable-gpu'] +
                 (['--blink-settings=imagesEnabled=false'] if headless else []))
        page = context.pages[0] if context.pages else context.new_page()
        page.goto('https://web.telegram.org/k/#' + PEER, wait_until='domcontentloaded', timeout=90000)
        baseline = True
        while True:
            try:
                if not page.url.startswith('https://web.telegram.org/k/'):
                    status('wrong_page')
                    baseline = True
                elif not page.evaluate('navigator.onLine'):
                    status('offline')
                    baseline = True
                else:
                    result = page.evaluate(EXTRACT, {'peer':PEER, 'title':TITLE})
                    if result['state'] == 'observing':
                        counts = record(db, result['messages'], baseline, export=os.environ.get('PAPER_EXPORT') == '1')
                        if os.environ.get('PAPER_EXPORT') == '1':
                            flush_exports(db, '/export')
                        baseline = False
                        status('observing', last_scan=time.time(), counts=counts,
                            stored=db.execute('SELECT COUNT(*) FROM messages').fetchone()[0])
                    else:
                        baseline = True
                        status(result['state'], diagnostics=result.get('diagnostics', {}))
            except Exception as exc:
                # Exception text can contain DOM/content; log type only.
                status('browser_error', error_type=type(exc).__name__)
                baseline = True
                if page.is_closed():
                    raise RuntimeError('browser closed') from None
            time.sleep(20)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'status':
        print((STATE / 'status.json').read_text())
    elif len(sys.argv) > 1 and sys.argv[1] == 'health':
        data = json.loads((STATE / 'status.json').read_text())
        sys.exit(0 if time.time() - data['heartbeat'] < 120 else 1)
    else:
        run()
