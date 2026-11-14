(() => {
  if (globalThis.__cs2ToolkitMedia) return;
  globalThis.__cs2ToolkitMedia = true;
  const api = globalThis.browser || chrome;
  let mode = 'free', seq = -1, epoch = '', queued = false;
  const observer = new MutationObserver(changes => {
    if (mode === 'free') return;
    if (changes.some(change => [...change.addedNodes].some(node => node.nodeType === 1 &&
        (node.matches('video,audio') || node.querySelector('video,audio'))))) schedule();
  });
  function schedule() {
    if (queued) return;
    queued = true;
    queueMicrotask(() => { queued = false; apply(); });
  }
  async function apply() {
    const media = [...document.querySelectorAll('video,audio')];
    if (mode === 'pause') media.forEach(element => { if (!element.paused) element.pause(); });
    if (mode === 'watch' && media.length) {
      const element = media.slice().sort((a, b) => b.clientWidth * b.clientHeight - a.clientWidth * a.clientHeight)[0];
      if (element.paused) {
        const started = seq;
        const startedEpoch = epoch;
        try {
          await element.play();
          if ((started !== seq || startedEpoch !== epoch) && mode !== 'watch') element.pause();
        } catch { return {handled: false, reason: 'autoplay_blocked', media_count: media.length}; }
      }
    }
    return {handled: true, media_count: media.length};
  }
  function policy(message) {
    if (message.epoch === epoch && message.seq < seq) return Promise.resolve({handled: true, media_count: 0});
    epoch = message.epoch;
    seq = message.seq; mode = message.mode;
    observer.disconnect();
    if (mode !== 'free') observer.observe(document, {childList: true, subtree: true});
    return apply();
  }
  for (const name of ['play', 'playing', 'loadedmetadata', 'canplay']) {
    document.addEventListener(name, event => {
      if (mode === 'pause' && event.target?.pause) event.target.pause();
      else if (mode === 'watch' && name !== 'play' && name !== 'playing') schedule();
    }, true);
  }
  api.runtime.onMessage.addListener((message, sender, respond) => {
    if (message.type !== 'policy' || !['watch', 'pause', 'free'].includes(message.mode)) return;
    policy(message).then(respond);
    return true;
  });
  api.runtime.sendMessage({type: 'get_policy'}).then(message => {
    if (message?.type === 'policy') policy(message);
  }).catch(() => {});
})();
