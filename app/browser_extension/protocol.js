/* Shared, browser-independent binding rules; also exercised by Node tests. */
(function (root) {
  function site(value) {
    try {
      const url = new URL(value);
      if (!['http:', 'https:'].includes(url.protocol)) return '';
      const host = url.hostname.toLowerCase().replace(/^www\./, '');
      for (const base of ['bilibili.com', 'douyin.com', 'youtube.com']) {
        if (host === base || host.endsWith('.' + base)) return base;
      }
      return host;
    } catch { return ''; }
  }
  function sameSite(left, right) { return !!site(left) && site(left) === site(right); }
  function chooseTab(tabs, url, previous) {
    const candidates = tabs.filter(tab => sameSite(tab.url || tab.pendingUrl, url));
    const remembered = candidates.find(tab => tab.id === previous);
    if (remembered) return remembered;
    const parsed = new URL(url);
    if (parsed.pathname !== '/') return candidates.find(tab => tab.url === url);
    return candidates.find(tab => tab.active) || candidates.find(tab => tab.audible) ||
      candidates.sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0))[0];
  }
  function credentials(value) {
    try {
      const url = new URL(value.url);
      return url.protocol === 'ws:' && url.hostname === '127.0.0.1' && !!url.port &&
        url.pathname === '/' && !url.username && !url.password && !url.search && !url.hash &&
        /^[A-Za-z0-9_-]{32,128}$/.test(value.token);
    } catch { return false; }
  }
  const protocol = {site, sameSite, chooseTab, credentials};
  root.CTMediaProtocol = protocol;
  if (typeof module !== 'undefined') module.exports = protocol;
})(globalThis);
