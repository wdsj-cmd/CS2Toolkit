const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const folder = path.resolve(__dirname, '../app/browser_extension');
const P = require(path.join(folder, 'protocol.js'));
const flush = async () => { for (let i = 0; i < 10; i++) await new Promise(setImmediate); };
const event = () => ({listeners: [], addListener(fn) { this.listeners.push(fn); }});

test('site boundaries reject impostors and unsafe schemes', () => {
  assert(P.sameSite('https://www.bilibili.com/', 'https://player.bilibili.com/video/1'));
  assert(!P.sameSite('https://bilibili.com/', 'https://bilibili.com.evil.com/'));
  assert(!P.site('javascript:alert(1)'));
});
test('remembered video tab wins over homepage and other music tabs', () => {
  const tabs = [{id: 1, active: true, url: 'https://bilibili.com/'},
    {id: 2, url: 'https://bilibili.com/video/1'}, {id: 3, audible: true, url: 'https://other.example/music'}];
  assert.equal(P.chooseTab(tabs, 'https://bilibili.com/', 2).id, 2);
  assert.equal(P.chooseTab(tabs, 'https://other.example/', null).id, 3);
  assert.equal(P.chooseTab(tabs, 'https://other.example/new', null), undefined);
});
test('only authenticated loopback websocket pairing parameters are accepted', () => {
  const value = {url: 'ws://127.0.0.1:12345', token: 'a'.repeat(43)};
  assert(P.credentials(value));
  for (const url of ['ws://evil.com:12345', 'wss://127.0.0.1:12345', 'ws://user@127.0.0.1:12345',
    'ws://127.0.0.1:12345/path']) assert(!P.credentials({...value, url}));
});

async function background(tabs, previous) {
  const messages = [], focused = [], created = [], removed = [], sockets = [];
  const storage = {pairing: {url: 'ws://127.0.0.1:12345', token: 'a'.repeat(43)}, binding: previous};
  const api = {
    runtime: {onMessage: event(), onStartup: event(), getManifest: () => ({version: '0.1.0'}), reload() {}},
    permissions: {getAll: async () => ({origins: ['*://*.bilibili.com/*']})},
    storage: {local: {get: async () => storage, set: async value => Object.assign(storage, value)}},
    scripting: {executeScript: async () => []},
    windows: {update: async (id, value) => focused.push({id, value})},
    tabs: {onCreated: event(), onUpdated: event(), onActivated: event(), onRemoved: event(),
      query: async () => tabs,
      get: async id => { const tab = tabs.find(t => t.id === id); if (!tab) throw Error('closed'); return tab; },
      create: async value => { const tab = {id: 50, windowId: 1, ...value}; created.push(tab); tabs.push(tab); return tab; },
      remove: async id => removed.push(id),
      update: async (id, value) => { Object.assign(tabs.find(t => t.id === id), value); },
      sendMessage: async (id, message) => { messages.push({id, message: {...message}}); return {handled: true, media_count: 1}; }}
  };
  class Socket {
    static OPEN = 1;
    constructor() { this.readyState = 1; sockets.push(this); setImmediate(() => this.onopen?.()); }
    send(text) {
      const value = JSON.parse(text);
      if (value.type === 'hello') setImmediate(() => {
        this.emit({type: 'authenticated'});
        this.emit({type: 'state', seq: 0, epoch: 'native', url: 'https://www.bilibili.com/', mode: 'free', open: false});
      });
    }
    emit(value) { this.onmessage?.({data: JSON.stringify(value)}); }
    close() { this.readyState = 3; this.onclose?.(); }
  }
  const context = {chrome: api, CTMediaProtocol: P, WebSocket: Socket, navigator: {userAgent: 'Chrome/130'},
    crypto: require('node:crypto').webcrypto,
    URL, console, setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1, clearInterval() {}};
  vm.runInNewContext(fs.readFileSync(path.join(folder, 'background.js'), 'utf8'), context);
  await flush();
  return {api, messages, focused, created, removed, sockets,
    command(value) { sockets.at(-1).emit({type: 'state', epoch: 'native', url: 'https://www.bilibili.com/', ...value}); }};
}
test('bound video is reused, a child video follows it, pause affects only owned tabs', async () => {
  const tabs = [{id: 1, windowId: 1, active: false, url: 'https://other.example/music'},
    {id: 2, windowId: 1, active: true, url: 'https://www.bilibili.com/video/1'}];
  const bg = await background(tabs, {url: 'https://www.bilibili.com/', primary: 2, tabs: [2]});
  bg.command({seq: 1, mode: 'watch', open: true}); await flush();
  assert.equal(bg.created.length, 0);
  const child = {id: 3, windowId: 1, active: true, openerTabId: 2, url: 'https://www.bilibili.com/video/2'};
  tabs.push(child);
  bg.api.tabs.onCreated.listeners[0](child); await flush();
  bg.messages.length = 0;
  bg.command({seq: 2, mode: 'pause', open: false}); await flush();
  assert.deepEqual(bg.messages.map(x => x.id).sort(), [2, 3]);
  assert(bg.messages.every(x => x.message.mode === 'pause'));
  bg.messages.length = 0;
  bg.sockets.at(-1).close(); await flush();
  assert(bg.messages.every(x => x.message.mode === 'free'));
});
test('a pause during asynchronous tab creation closes the stale created tab without activating it', async () => {
  const bg = await background([], {url: '', primary: null, tabs: []});
  let finish;
  bg.api.tabs.create = () => new Promise(resolve => { finish = resolve; });
  bg.command({seq: 1, mode: 'watch', open: true}); await flush();
  assert.equal(typeof finish, 'function');
  bg.command({seq: 2, mode: 'pause', open: false}); await flush();
  finish({id: 50, windowId: 1, url: 'https://www.bilibili.com/'}); await flush();
  assert.deepEqual(bg.removed, [50]);
  assert.equal(bg.focused.length, 0);
});

function content() {
  const elements = [], handlers = {}, observers = [], listeners = [];
  let resolvePlay;
  const element = {paused: true, pauses: 0, plays: 0, clientWidth: 100, clientHeight: 100,
    pause() { this.paused = true; this.pauses++; },
    play() { this.paused = false; this.plays++; return new Promise(resolve => { resolvePlay = resolve; }); }};
  elements.push(element);
  const document = {querySelectorAll: () => elements,
    addEventListener(name, listener) { handlers[name] = listener; }};
  class Observer { constructor(fn) { this.fn = fn; observers.push(this); } observe() {} disconnect() {} }
  const chrome = {runtime: {onMessage: {addListener(fn) { listeners.push(fn); }},
    sendMessage: async () => ({type: 'policy', epoch: 'one', mode: 'free', seq: 0})}};
  vm.runInNewContext(fs.readFileSync(path.join(folder, 'content.js'), 'utf8'),
    {chrome, MutationObserver: Observer, document, queueMicrotask, console});
  return {element, elements, handlers, observers, resolve: () => resolvePlay?.(),
    policy(mode, seq, epoch = 'one') { return new Promise(resolve => listeners[0]({type: 'policy', mode, seq, epoch}, {}, resolve)); }};
}
test('late-created media and autoplay are stopped until foreground policy releases the page', async () => {
  const c = content(); await flush();
  await c.policy('pause', 1);
  const late = {...c.element, paused: false, pause: c.element.pause};
  c.elements.push(late);
  c.observers[0].fn([{addedNodes: [{nodeType: 1, matches: () => true}]}]); await flush();
  assert(late.paused);
  c.element.paused = false;
  c.handlers.play({target: c.element}); assert(c.element.paused);
  await c.policy('free', 2);
  c.element.paused = false;
  c.handlers.play({target: c.element}); assert(!c.element.paused);
});
test('stale play completion and stale initial policy cannot undo a new pause', async () => {
  const c = content(); await flush();
  const play = c.policy('watch', 1);
  await c.policy('pause', 2);
  await c.policy('free', 0);
  c.element.paused = false;
  c.resolve(); await play;
  assert(c.element.paused);
  await c.policy('free', 0, 'restarted-app');
  c.element.paused = false;
  c.handlers.play({target: c.element}); assert(!c.element.paused);
});
