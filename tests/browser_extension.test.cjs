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
  const messages = [], focused = [], created = [], removed = [], sockets = [], sent = [];
  const windows = [{id: 1, state: 'normal', focused: true}];
  const storage = {pairing: {url: 'ws://127.0.0.1:12345', token: 'a'.repeat(43)}, binding: previous};
  const api = {
    runtime: {onMessage: event(), onStartup: event(), getManifest: () => ({version: '0.1.0'}), reload() {}},
    permissions: {getAll: async () => ({origins: ['*://*.bilibili.com/*']})},
    storage: {local: {get: async () => storage, set: async value => Object.assign(storage, value)}},
    scripting: {executeScript: async () => []},
    windows: {
      getAll: async () => windows,
      get: async id => windows.find(window => window.id === id),
      create: async value => {
        const tab = {id: 50, windowId: 2, url: value.url};
        const window = {id: 2, state: 'normal', tabs: [tab]};
        windows.push(window);tabs.push(tab);created.push(tab);return window;
      },
      update: async (id, value) => {
        const window = windows.find(window => window.id === id);
        if (value.focused && window.state === 'minimized') throw Error('Cannot focus minimized window');
        Object.assign(window, value);focused.push({id, value});
      }},
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
      sent.push(value);
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
  return {api, messages, focused, created, removed, sockets, storage, windows, sent,
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
test('changing the configured website releases old paused tabs without opening a new page', async () => {
  const tabs = [{id: 2, windowId: 1, active: true, url: 'https://www.bilibili.com/video/1'}];
  const bg = await background(tabs, {url: '', primary: null, tabs: []});
  bg.command({seq: 1, mode: 'watch', open: true}); await flush();
  assert.equal(bg.storage.binding.url, 'https://www.bilibili.com/');
  bg.command({seq: 2, mode: 'pause', open: false}); await flush();
  bg.messages.length = 0;
  bg.command({seq: 3, mode: 'free', open: false, url: 'https://video.example/watch'}); await flush();
  assert.deepEqual(bg.messages.map(item => item.id), [2]);
  assert.equal(bg.messages[0].message.mode, 'free');
  assert.equal(bg.created.length, 0);
  assert.equal(bg.storage.binding.url, 'https://video.example/watch');
});
test('a minimized browser window is restored before tab activation and focus', async () => {
  const bg = await background([{id: 2, windowId: 1, url: 'https://www.bilibili.com/video/1'}],
    {url: 'https://www.bilibili.com/', primary: 2, tabs: [2]});
  bg.windows[0].state = 'minimized';
  bg.command({seq: 1, mode: 'watch', open: true}); await flush();
  assert.equal(bg.focused[0].value.state, 'normal');
  assert.equal(bg.focused.at(-1).value.focused, true);
  assert(bg.sent.some(message => message.type === 'ack' && message.navigation_ready && message.handled));
});
test('a browser with no windows gets an explicit new window instead of relying on current-window tab creation', async () => {
  const bg = await background([], {url: '', primary: null, tabs: []});
  bg.windows.length = 0;
  bg.api.tabs.create = async () => { throw Error('No current window'); };
  bg.command({seq: 1, mode: 'watch', open: true}); await flush();
  assert.equal(bg.created.length, 1);
  assert.equal(bg.created[0].windowId, 2);
  assert(bg.sent.some(message => message.type === 'ack' && message.navigation_ready));
});
test('window activation failure identifies the failing stage without logging browsing URLs', async () => {
  const bg = await background([{id: 2, windowId: 1, url: 'https://www.bilibili.com/video/1'}],
    {url: 'https://www.bilibili.com/', primary: 2, tabs: [2]});
  bg.api.windows.update = async () => { throw Error('No window with id'); };
  bg.command({seq: 1, mode: 'watch', open: true}); await flush();
  const failure = bg.sent.find(message => message.reason === 'command_failed');
  assert.equal(failure.stage, 'window_focus');
  assert.equal(failure.error_code, 'window_closed');
  assert(!Object.hasOwn(failure, 'url'));
});
test('last known maximized browser state survives a later minimized-window reopen', async () => {
  const bg = await background([{id: 2, windowId: 1, url: 'https://www.bilibili.com/video/1'}],
    {url: 'https://www.bilibili.com/', primary: 2, tabs: [2]});
  bg.windows[0].state = 'maximized';
  bg.command({seq: 1, mode: 'watch', open: true}); await flush();
  bg.windows[0].state = 'minimized';bg.focused.length = 0;
  bg.command({seq: 2, mode: 'watch', open: true}); await flush();
  assert.equal(bg.focused[0].value.state, 'maximized');
});

function content() {
  const elements = [], handlers = {}, observers = [], listeners = [], timers = new Map(), reports = [];
  let clock = 0, nextTimer = 0;
  let resolvePlay;
  const element = {paused: true, pauses: 0, plays: 0, clientWidth: 100, clientHeight: 100,
    tagName: 'VIDEO',
    getBoundingClientRect: () => ({left: 0, top: 0, right: 100, bottom: 100}),
    pause() { this.paused = true; this.pauses++; },
    play() { this.paused = false; this.plays++; return new Promise(resolve => { resolvePlay = resolve; }); }};
  elements.push(element);
  const document = {visibilityState: 'visible', querySelectorAll: () => elements,
    addEventListener(name, listener) { handlers[name] = listener; },
    removeEventListener(name, listener) { if (handlers[name] === listener) delete handlers[name]; }};
  class Observer { constructor(fn) { this.fn = fn; observers.push(this); } observe() {} disconnect() {} }
  const chrome = {runtime: {onMessage: {addListener(fn) { listeners.push(fn); },
      removeListener(fn) { listeners.splice(listeners.indexOf(fn), 1); }},
    sendMessage: async value => {
      if (value.type === 'get_policy') return {type: 'policy', epoch: 'one', mode: 'free', seq: 0};
      reports.push(value);return {ok: true};
    }}};
  const context = {chrome, MutationObserver: Observer, document, queueMicrotask, console,
    innerWidth: 1920, innerHeight: 1080,
    getComputedStyle: item => ({display: item.hidden ? 'none' : 'block', visibility: 'visible', opacity: '1'}),
    Date: {now: () => clock},
    setTimeout: (fn, delay) => { const id = ++nextTimer;timers.set(id, {fn, at: clock + delay});return id; },
    clearTimeout: id => timers.delete(id)};
  const compiled = vm.createContext(context);
  const install = () => vm.runInContext(fs.readFileSync(path.join(folder, 'content.js'), 'utf8'), compiled);
  install();
  return {element, elements, handlers, observers, document, reports, install, listeners,
    dispose: () => context.__cs2ToolkitMedia.dispose(),
    async advance(ms) {
      clock += ms;
      for (const [id, timer] of [...timers]) if (timer.at <= clock) { timers.delete(id); timer.fn(); }
      await flush();
    },
    resolve: () => resolvePlay?.(),
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

test('watch plays the visible short video instead of a larger hidden preload', async () => {
  const c = content();await flush();
  c.element.hidden = true;
  const current = {...c.element, hidden: false, plays: 0,
    play() { this.paused = false;this.plays++;return Promise.resolve(); }};
  c.elements.push(current);
  await c.policy('watch', 1);
  assert.equal(c.element.plays, 0);
  assert.equal(current.plays, 1);
});

test('offscreen preloads are ignored even when they have the same dimensions', async () => {
  const c = content();await flush();
  c.element.getBoundingClientRect = () => ({left: 0, top: -200, right: 100, bottom: -100});
  const current = {...c.element, getBoundingClientRect: () => ({left: 0, top: 0, right: 100, bottom: 100}),
    play() { this.paused = false;return Promise.resolve(); }};
  c.elements.push(current);
  await c.policy('watch', 1);
  assert.equal(c.element.plays, 0);
  assert.equal(current.paused, false);
});

test('a page hidden during activation starts its visible video after visibility changes', async () => {
  const c = content();await flush();
  c.element.play = function() { this.paused = false;this.plays++;return Promise.resolve(); };
  c.document.visibilityState = 'hidden';
  const result = await c.policy('watch', 1);
  assert.equal(result.reason, 'page_hidden');assert.equal(c.element.plays, 0);
  c.document.visibilityState = 'visible';c.handlers.visibilitychange();await flush();
  assert.equal(c.element.plays, 1);
});

test('a site pausing during foreground restoration is retried briefly, then user pause is respected', async () => {
  const c = content();await flush();
  c.element.play = function() { this.paused = false;this.plays++;return Promise.resolve(); };
  await c.policy('watch', 1);
  c.element.pause();c.handlers.pause({target: c.element});await flush();
  assert.equal(c.element.plays, 2);
  await c.advance(2600);
  c.element.pause();c.handlers.pause({target: c.element});await flush();
  assert.equal(c.element.plays, 2);
});

test('returning to the game cancels pending visibility retries and prevents late playback', async () => {
  const c = content();await flush();
  c.element.play = () => Promise.reject(Object.assign(Error('blocked'), {name: 'NotAllowedError'}));
  assert.equal((await c.policy('watch', 1)).reason, 'play_retry_pending');
  await c.policy('pause', 2);await c.advance(3000);
  assert(c.element.paused);assert(!c.reports.some(value => value.seq === 1));
});

test('persistent autoplay rejection is reported instead of claiming a successful playback', async () => {
  const c = content();await flush();
  c.element.play = () => Promise.reject(Object.assign(Error('blocked'), {name: 'NotAllowedError'}));
  const pending = await c.policy('watch', 1);
  assert.equal(pending.handled, true);
  await c.advance(2600);
  const failed = c.reports.find(value => value.reason === 'autoplay_blocked');
  assert(failed);assert.equal(failed.handled, false);
});

test('reinjecting the same script is idempotent and disposal removes timers and policy handlers', async () => {
  const c = content();await flush();
  c.install();assert.equal(c.listeners.length, 1);
  c.dispose();assert.equal(c.listeners.length, 0);
  await c.advance(3000);assert.equal(c.element.plays, 0);
});

test('a primary tab playback failure cannot be masked by a successful secondary tab', async () => {
  const bg = await background([{id: 2, windowId: 1, url: 'https://www.bilibili.com/video/1'},
    {id: 3, windowId: 1, url: 'https://www.bilibili.com/video/2'}],
    {url: 'https://www.bilibili.com/', primary: 2, tabs: [2, 3]});
  bg.api.tabs.sendMessage = async id => id === 2 ?
    {handled: false, reason: 'autoplay_blocked', media_count: 1} : {handled: true, media_count: 1};
  bg.command({seq: 1, mode: 'watch', open: true});await flush();
  const result = bg.sent.find(value => value.seq === 1 && value.type === 'ack');
  assert.equal(result.handled, false);assert.equal(result.reason, 'autoplay_blocked');
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
