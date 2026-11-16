(() => {
  if (globalThis.__cs2ToolkitMedia?.version === 3) return;
  globalThis.__cs2ToolkitMedia?.dispose?.();
  const api = globalThis.browser || chrome;
  let mode = 'free', seq = -1, epoch = '', queued = false;
  let retryTimer, watchUntil = 0, disposed = false, lastReport = '';
  const observer = new MutationObserver(changes => {
    if (mode === 'free') return;
    if (changes.some(change => [...change.addedNodes].some(node => node.nodeType === 1 &&
        (node.matches('video,audio') || node.querySelector('video,audio'))))) schedule();
  });
  function schedule() {
    if (queued || disposed) return;
    queued = true;
    queueMicrotask(() => {
      queued = false;
      if (disposed) return;
      const started = seq, startedEpoch = epoch;
      apply().then(result => {
        if (disposed || started !== seq || startedEpoch !== epoch) return;
        const signature = JSON.stringify([seq, epoch, result]);
        if (signature === lastReport) return;
        lastReport = signature;
        api.runtime.sendMessage({type: 'policy_result', seq, epoch, ...result}).catch(() => {});
      }).catch(() => {});
    });
  }
  function visibleArea(element) {
    if (element.checkVisibility && !element.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) return 0;
    const style = getComputedStyle(element);
    if (style.display === 'none' || style.visibility !== 'visible' || Number(style.opacity) === 0) return 0;
    const rect = element.getBoundingClientRect();
    return Math.max(0, Math.min(rect.right, innerWidth) - Math.max(rect.left, 0)) *
      Math.max(0, Math.min(rect.bottom, innerHeight) - Math.max(rect.top, 0));
  }
  function retryWatch() {
    clearTimeout(retryTimer);
    if (mode === 'watch' && Date.now() < watchUntil && !disposed) {
      retryTimer = setTimeout(schedule, Math.min(250, watchUntil - Date.now()));
    }
  }
  async function apply() {
    if (disposed) return {handled: true, media_count: 0};
    const media = [...document.querySelectorAll('video,audio')];
    if (mode === 'pause') media.forEach(element => { if (!element.paused) element.pause(); });
    if (mode === 'watch') {
      retryWatch();
      if (document.visibilityState === 'hidden') return {handled: true, reason: 'page_hidden', media_count: media.length};
      const visible = media.map(element => ({element, area: visibleArea(element)})).filter(item => item.area > 0)
        .sort((a, b) => b.area - a.area);
      const element = visible[0]?.element || media.find(item => item.tagName === 'AUDIO' && media.every(item => item.tagName !== 'VIDEO'));
      if (!element) return {handled: true, reason: 'waiting_visible_media', media_count: media.length};
      if (element.paused) {
        const started = seq;
        const startedEpoch = epoch;
        try {
          await element.play();
          if (disposed || ((started !== seq || startedEpoch !== epoch) && mode !== 'watch')) element.pause();
        } catch (error) {
          const retrying = Date.now() < watchUntil;
          return {handled: retrying, reason: retrying ? 'play_retry_pending' :
            error?.name === 'NotAllowedError' ? 'autoplay_blocked' : 'play_failed', media_count: media.length};
        }
      }
      return {handled: !element.paused || Date.now() < watchUntil,
        reason: element.paused ? 'play_retry_pending' : 'playing_visible_media', media_count: media.length};
    }
    return {handled: true, media_count: media.length};
  }
  function policy(message) {
    if (disposed) return Promise.resolve({handled: true, media_count: 0});
    if (message.epoch === epoch && message.seq < seq) return Promise.resolve({handled: true, media_count: 0});
    epoch = message.epoch;
    seq = message.seq; mode = message.mode;
    clearTimeout(retryTimer);
    watchUntil = mode === 'watch' ? Date.now() + 2500 : 0;
    observer.disconnect();
    if (mode !== 'free') observer.observe(document, {childList: true, subtree: true});
    return apply();
  }
  const listeners = [];
  function listen(target, name, callback) {
    target.addEventListener(name, callback, true);
    listeners.push(() => target.removeEventListener(name, callback, true));
  }
  for (const name of ['play', 'playing', 'loadedmetadata', 'canplay', 'pause']) {
    listen(document, name, event => {
      if (mode === 'pause' && event.target?.pause) event.target.pause();
      else if (mode === 'watch' && (name === 'loadedmetadata' || name === 'canplay'
               || (name === 'pause' && Date.now() < watchUntil))) schedule();
    });
  }
  listen(document, 'visibilitychange', () => {
    if (mode === 'watch' && document.visibilityState !== 'hidden') {
      watchUntil = Date.now() + 2500;
      schedule();
    }
  });
  const onMessage = (message, sender, respond) => {
    if (message.type !== 'policy' || !['watch', 'pause', 'free'].includes(message.mode)) return;
    policy(message).then(respond);
    return true;
  };
  api.runtime.onMessage.addListener(onMessage);
  globalThis.__cs2ToolkitMedia = {version: 3, dispose() {
    disposed = true; mode = 'free'; clearTimeout(retryTimer); observer.disconnect();
    listeners.forEach(remove => remove()); api.runtime.onMessage.removeListener(onMessage);
  }};
  api.runtime.sendMessage({type: 'get_policy'}).then(message => {
    if (message?.type === 'policy') policy(message);
  }).catch(() => {});
})();
