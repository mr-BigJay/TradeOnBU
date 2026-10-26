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
 const norm = s => s.replace(/[\u200e\u200f]/g, '').replace(/\s+/g, ' ').trim();
 const diagnostics = {
   ready:document.readyState, visible_headers:headers.length,
   body_elements:document.body?.children.length || 0,
   visible_text_chars:document.body?.innerText.length || 0,
   qr_login_hint:/log in.*qr|scan.*qr|qr.*code/i.test(document.body?.innerText || ''),
   webgl_hint:/webgl/i.test(document.body?.innerText || ''),
   unsupported_hint:/unsupported browser|browser.*not supported|update your browser/i.test(document.body?.innerText || ''),
   matching_headers:headers.filter(e=>norm(e.innerText)===norm(title)).length,
   expected_route:location.hash.split('_')[0] === '#' + peer,
   login_fields:[...document.querySelectorAll('input[type=tel], input[type=password]')].filter(visible).length,
   visible_message_roots:[...document.querySelectorAll('.bubbles-inner')].filter(visible).length
 };
 if (diagnostics.login_fields > 0) return {state:'login_required', messages:[], diagnostics};
 if (headers.length !== 1 || norm(headers[0].innerText) !== norm(title))
   return {state:'waiting_for_channel', messages:[], diagnostics};
 if (location.hash.split('_')[0] !== '#' + peer)
   return {state:'wrong_channel', messages:[], diagnostics};
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


OPEN_TARGET = r"""({peer, title}) => {
 const visible = e => !!e.getClientRects().length;
 const matches = [...document.querySelectorAll('.chatlist [data-peer-id], #column-left a[href]')]
   .filter(visible).filter(e => (e.dataset.peerId === peer || e.getAttribute('href') === '#'+peer)
     && e.innerText.includes(title));
 const candidates = [...new Set(matches.map(e=>e.closest('a') || e))];
 if (candidates.length !== 1) return {clicked:false, candidates:candidates.length};
 candidates[0].click();
 return {clicked:true, candidates:1};
}"""


def enable_power_saving(page):
    """Use Telegram's visible settings controls, never private app stores/APIs."""
    menu = page.locator('#column-left .btn-menu-toggle:not(.btn-new-menu):visible')
    if menu.count() != 1:
        raise RuntimeError('menu_not_unique')
    menu.click(timeout=10000)
    page.locator('.btn-menu-item .tgico-settings:visible').click(timeout=10000)
    page.locator('.tgico-general_filled:visible').click(timeout=10000)
    page.locator('.tgico-sputnik_filled:visible').click(timeout=10000)
    toggle = page.locator('.power-saving-container #input-power-saving-all')
    toggle.wait_for(state='attached', timeout=10000)
    if not toggle.is_checked():
        page.locator('.power-saving-container label').filter(has=toggle).click(timeout=10000)
    if not toggle.is_checked():
        raise RuntimeError('power_saving_not_checked')
    # Telegram's visible control delays its settings update by 200ms.
    page.wait_for_timeout(1500)
    return True


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
                if not baseline and not old and 0 <= now - published <= 120:
                    payload = dict(key=key, peer=PEER, body=body, published=published, received=now, kind='new', has_reply=bool(msg.get('has_reply')))
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
            # Keep normal rendering during boot: disabling all images/graphics can
            # prevent a web app's initialization promises from completing.
            args=['--disable-dev-shm-usage'])
        page = context.pages[0] if context.pages else context.new_page()
        page_errors = [0]
        failed_requests = [0]
        page.on('pageerror', lambda error: page_errors.__setitem__(0, page_errors[0] + 1))
        page.on('requestfailed', lambda request: failed_requests.__setitem__(0, failed_requests[0] + 1))
        status('loading_page')
        page.goto('https://web.telegram.org/k/', wait_until='domcontentloaded', timeout=120000)
        opened_at = time.monotonic()
        attempts = 0
        baseline = True
        power_marker = STATE / 'power-saving-v1.json'
        power_verified = power_marker.exists()
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
                        if not power_verified:
                            status('configuring_power_saving')
                            try:
                                enable_power_saving(page)
                            except Exception as exc:
                                status('power_saving_failed', error_type=type(exc).__name__)
                                # Let the wrapper read the diagnostic and stop safely.
                                while True:
                                    time.sleep(20)
                                    status('power_saving_failed')
                            power_marker.write_text(json.dumps({'verified_at':time.time()}))
                            power_verified = True
                            baseline = True
                            attempts = 0
                            page.goto('https://web.telegram.org/k/#' + PEER, wait_until='domcontentloaded', timeout=60000)
                            page.reload(wait_until='domcontentloaded', timeout=120000)
                            opened_at = time.monotonic()
                            continue
                        counts = record(db, result['messages'], baseline, export=os.environ.get('PAPER_EXPORT') == '1')
                        if os.environ.get('PAPER_EXPORT') == '1':
                            flush_exports(db, '/export')
                        baseline = False
                        status('observing', power_saving='verified_in_profile', last_scan=time.time(), counts=counts,
                            stored=db.execute('SELECT COUNT(*) FROM messages').fetchone()[0])
                    else:
                        baseline = True
                        diagnostics = result.get('diagnostics', {})
                        diagnostics['javascript_error_count'] = page_errors[0]
                        diagnostics['failed_request_count'] = failed_requests[0]
                        if result['state'] in ('waiting_for_channel', 'wrong_channel') and attempts < 3 and time.monotonic() - opened_at >= 40:
                            action = page.evaluate(OPEN_TARGET, {'peer':PEER, 'title':TITLE})
                            diagnostics['open_action'] = action
                            if not action['clicked']:
                                page.goto('https://web.telegram.org/k/#' + PEER, wait_until='domcontentloaded', timeout=60000)
                            attempts += 1
                            opened_at = time.monotonic()
                        diagnostics['open_attempts'] = attempts
                        status(result['state'], diagnostics=diagnostics)
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
