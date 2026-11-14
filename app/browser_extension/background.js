/* No global media keys: commands reach only the remembered tab and its videos. */
if (typeof CTMediaProtocol === 'undefined') importScripts('protocol.js');
const api = globalThis.browser || chrome;
const P = CTMediaProtocol;
let socket, heartbeat, reconnect, pairing, ready = false;
let state = {seq: -1, mode: 'free', url: ''};
let binding = {url: '', primary: null, tabs: []};
let initialized;
let clientId;
const loaded = new Promise(resolve => { initialized = resolve; });

function send(message) {
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
}
async function persist() { await api.storage.local.set({binding}); }
function tabMode(id) {
  if (!binding.tabs.includes(id)) return 'free';
  return state.mode === 'watch' && id !== binding.primary ? 'pause' : state.mode;
}
async function applyTab(id) {
  try {
    const tab = await api.tabs.get(id);
    if (!P.sameSite(tab.url || tab.pendingUrl, state.url)) return {handled: true, media_count: 0};
    await api.scripting.executeScript({target: {tabId: id}, files: ['content.js']});
    return await api.tabs.sendMessage(id, {type: 'policy', mode: tabMode(id), seq: state.seq, epoch: state.epoch});
  } catch { return {handled: false, reason: 'tab_not_ready', media_count: 0}; }
}
async function applyAll() {
  const seq = state.seq;
  const results = await Promise.all(binding.tabs.map(applyTab));
  if (seq !== state.seq) return;
  const handled = results.length === 0 || results.some(result => result?.handled);
  send({type: 'ack', seq, handled,
    reason: handled ? 'tab_policy_applied' : 'tab_not_ready',
    media_count: results.reduce((sum, result) => sum + (result?.media_count || 0), 0)});
}
async function command(next) {
  if (!Number.isInteger(next.seq) || next.seq < state.seq ||
      !['watch', 'pause', 'free'].includes(next.mode)) return;
  state = next;
  const seq = next.seq;
  if (binding.url && binding.url !== next.url) {
    const previous = binding.tabs;
    binding = {url: next.url, primary: null, tabs: []};
    await Promise.all(previous.map(id => api.tabs.sendMessage(id,
      {type: 'policy', mode: 'free', seq, epoch: state.epoch}).catch(() => {})));
    if (seq !== state.seq) return;
    await persist();
  }
  if (next.open && next.mode === 'watch' && P.site(next.url)) {
    const tabs = await api.tabs.query({});
    if (seq !== state.seq) return;
    const previous = binding.url === next.url ? binding.primary : null;
    let tab = P.chooseTab(tabs, next.url, previous);
    const created = !tab;
    if (created) tab = await api.tabs.create({url: next.url, active: false});
    if (seq !== state.seq) {
      if (created) await api.tabs.remove(tab.id);
      return;
    }
    binding.url = next.url;
    binding.primary = tab.id;
    binding.tabs = [...new Set([...binding.tabs, tab.id])];
    await persist();
    // A pause arriving while the tab is created must not focus/play it later.
    if (seq === state.seq && state.mode === 'watch') {
      await api.tabs.update(tab.id, {active: true});
      if (seq === state.seq && state.mode === 'watch') await api.windows.update(tab.windowId, {focused: true});
    }
  }
  if (seq !== state.seq) return;
  await applyAll();
}
async function freeTabs() {
  state = {...state, mode: 'free', seq: state.seq + 1};
  await Promise.all(binding.tabs.map(id => api.tabs.sendMessage(id,
    {type: 'policy', mode: 'free', seq: state.seq, epoch: state.epoch}).catch(() => {})));
}
async function hello() {
  const permissions = await api.permissions.getAll();
  let browser = 'Chromium';
  if (api.runtime.getBrowserInfo) browser = (await api.runtime.getBrowserInfo()).name;
  else {
    const ua = navigator.userAgent;
    browser = /Edg\//.test(ua) ? 'Edge' : /OPR\//.test(ua) ? 'Opera' : 'Chrome / Chromium';
    if (navigator.brave && await navigator.brave.isBrave()) browser = 'Brave';
  }
  const process = ({Edge: 'msedge.exe', Firefox: 'firefox.exe', Opera: 'opera.exe', Brave: 'brave.exe'})[browser] || 'chrome.exe';
  send({type: 'hello', token: pairing.token, browser, client_id: clientId,
    version: api.runtime.getManifest().version, process, origins: permissions.origins || []});
}
function connect(value, save = false) {
  if (!P.credentials(value)) return Promise.resolve(false);
  clearTimeout(reconnect);
  if (socket) { socket.onclose = null; socket.close(); }
  const previousPairing = pairing;
  pairing = value;
  ready = false;
  state = {seq: -1, mode: 'free', url: ''};
  const current = socket = new WebSocket(value.url);
  return new Promise(resolve => {
    const timeout = setTimeout(() => { if (!ready && socket === current) current.close(); resolve(false); }, 4000);
    current.onopen = () => hello().catch(() => current.close());
    current.onmessage = async event => {
      if (current !== socket) return;
      let message;
      try { message = JSON.parse(event.data); } catch { return; }
      if (message.type === 'authenticated') {
        ready = true;
        clearTimeout(timeout);
        if (save) await api.storage.local.set({pairing: value});
        clearInterval(heartbeat);
        heartbeat = setInterval(() => send({type: 'ping'}), 20000);
        resolve(true);
      } else if (ready && message.type === 'state') {
        command(message).catch(() => send({type: 'ack', seq: message.seq, handled: false, reason: 'command_failed'}));
      } else if (ready && message.type === 'reload') {
        await freeTabs();
        api.runtime.reload();
      }
    };
    current.onerror = () => resolve(false);
    current.onclose = async () => {
      if (current !== socket) return;
      if (!ready && save) pairing = previousPairing;
      ready = false;
      clearInterval(heartbeat);
      await freeTabs();
      if (pairing) reconnect = setTimeout(() => connect(pairing), 1500);
      resolve(false);
    };
  });
}
async function follow(tab) {
  const url = tab.url || tab.pendingUrl;
  if (!ready || !P.sameSite(url, state.url)) return;
  const descendant = binding.tabs.includes(tab.openerTabId);
  const existing = binding.tabs.includes(tab.id);
  if (!descendant && !existing && !(state.mode === 'watch' && tab.active)) return;
  if (tab.active || descendant) binding.primary = tab.id;
  binding.tabs = [...new Set([...binding.tabs, tab.id])];
  await persist();
  await applyAll();
}
api.tabs.onCreated.addListener(tab => { loaded.then(() => follow(tab)); });
api.tabs.onUpdated.addListener((id, change, tab) => {
  if (change.url || change.status === 'complete') loaded.then(() => follow(tab));
});
api.tabs.onActivated.addListener(info => { loaded.then(async () => follow(await api.tabs.get(info.tabId))).catch(() => {}); });
api.tabs.onRemoved.addListener(id => {
  binding.tabs = binding.tabs.filter(tab => tab !== id);
  if (binding.primary === id) binding.primary = binding.tabs.at(-1) || null;
  persist();
});
api.runtime.onMessage.addListener((message, sender, respond) => {
  (async () => {
    await loaded;
    if (message.type === 'pair' && /^http:\/\/127\.0\.0\.1:\d+\/install(?:#|$)/.test(sender.url || '')) {
      return {ok: await connect(message.pairing, true)};
    }
    if (message.type === 'pair_status' && /^http:\/\/127\.0\.0\.1:\d+\/install(?:#|$)/.test(sender.url || '')) {
      return {ok: ready};
    }
    if (message.type === 'get_policy' && sender.tab) {
      return {type: 'policy', seq: state.seq,
        mode: P.sameSite(sender.tab.url, state.url) ? tabMode(sender.tab.id) : 'free', epoch: state.epoch};
    }
    if (message.type === 'status' && !sender.tab) return {ready, state, version: api.runtime.getManifest().version};
    if (message.type === 'bind' && !sender.tab) {
      const tab = await api.tabs.get(message.tabId);
      if (!P.sameSite(tab.url, state.url)) return {ok: false, reason: '请选择工具中设定的网站'};
      binding = {url: state.url, primary: tab.id, tabs: [tab.id]};
      await persist(); await applyAll(); return {ok: true};
    }
    if (message.type === 'permissions_changed' && !sender.tab) {
      send({type: 'permissions', origins: (await api.permissions.getAll()).origins || []});
      return {ok: true};
    }
    return {ok: false};
  })().then(respond, error => respond({ok: false, reason: String(error.message || error)}));
  return true;
});
api.runtime.onStartup.addListener(() => { loaded.then(() => pairing && connect(pairing)); });
(async () => {
  const saved = await api.storage.local.get(['pairing', 'binding', 'clientId']);
  clientId = saved.clientId || crypto.randomUUID();
  if (!saved.clientId) await api.storage.local.set({clientId});
  if (saved.binding?.tabs) binding = saved.binding;
  initialized();
  if (saved.pairing) connect(saved.pairing);
})();
