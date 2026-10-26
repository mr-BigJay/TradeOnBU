// Run with NODE_PATH pointing to frontend/node_modules after npm ci.
const {JSDOM} = require('jsdom');
const {execFileSync} = require('node:child_process');
const assert = require('node:assert/strict');
const extract = execFileSync('python', ['-c', 'from reader import EXTRACT; print(EXTRACT)'], {cwd:__dirname, encoding:'utf8'});
function scan(extra='', peer='-1878414449', title='BehDark (VIP)') {
 const dom = new JSDOM(`<div class="chat-info"><div class="user-title">${title}</div></div>
 <div class="scrollable"><div class="bubbles-inner">
 <div class="bubble" data-peer-id="${peer}" data-mid="123"><div class="message">Synthetic signal<span class="time">12:00</span></div></div>
 </div></div>${extra}`, {url:'https://web.telegram.org/k/#-1878414449', runScripts:'outside-only'});
 // jsdom has no layout/innerText: emulate visible fixture nodes only.
 dom.window.HTMLElement.prototype.getClientRects = function(){return [1]};
 Object.defineProperty(dom.window.HTMLElement.prototype, 'innerText', {get(){return this.textContent}});
 return dom.window.eval(`(${extract})`)({peer:'-1878414449', title:'BehDark (VIP)'});
}
assert.equal(scan().state,'observing');
const buffer='<div class="bubbles-remover-container"><div class="bubbles-remover bubbles-inner"><div class="bubble" data-peer-id="-999" data-mid="9"></div></div></div>';
const result=scan(buffer);
assert.equal(result.state,'observing');
assert.equal(result.messages.length,1);
assert.equal(result.messages[0].text,'Synthetic signal');
assert.equal(scan('<div class="bubbles-inner"></div>').state,'selector_mismatch');
assert.equal(scan('', '-999').state,'peer_mismatch');
assert.equal(scan('', '-1878414449','Other channel').state,'waiting_for_channel');
console.log('5 DOM regression cases passed');
