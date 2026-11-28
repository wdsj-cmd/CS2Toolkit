(() => {
  if (location.hostname !== '127.0.0.1' || location.pathname !== '/install') return;
  const start = async () => {
    if (!document.querySelector('meta[name="cs2toolkit-extension"][content="1"]')) return;
    const encoded = new URLSearchParams(location.hash.slice(1)).get('pair');
    try {
      const api = globalThis.browser || chrome;
      const result = encoded ? await api.runtime.sendMessage({type: 'pair',
        pairing: JSON.parse(atob(encoded.replace(/-/g, '+').replace(/_/g, '/')))}) :
        await api.runtime.sendMessage({type: 'pair_status'});
      if (result?.ok && encoded) history.replaceState(null, '', location.pathname);
      document.dispatchEvent(new CustomEvent('cs2toolkit-paired', {detail: !!result?.ok, bubbles: true}));
    } catch {
      document.dispatchEvent(new CustomEvent('cs2toolkit-paired', {detail: false, bubbles: true}));
    }
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once: true});
  else start();
})();
