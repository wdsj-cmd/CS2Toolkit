const api = globalThis.browser || chrome;
const status = document.getElementById('status');
let connection;
async function refresh() {
  connection = await api.runtime.sendMessage({type: 'status'});
  status.textContent = connection.ready ? '已连接工具 · ' + (connection.state.url || '等待目标网站') : '未连接，请打开工具中的安装引导进行连接。';
}
document.getElementById('grant').onclick = async () => {
  try {
    if (!connection?.ready || !connection.state.url) throw Error('请先连接工具并设置目标网页');
    const url = new URL(connection.state.url);
    if (!['http:', 'https:'].includes(url.protocol)) throw Error('请先在工具中设置网页');
    const origin = url.origin + '/*';
    if (await api.permissions.request({origins: [origin]})) {
      const id = 'site-' + url.hostname.replace(/[^a-z0-9]/gi, '-');
      const registered = await api.scripting.getRegisteredContentScripts();
      if (!registered.some(item => item.id === id)) await api.scripting.registerContentScripts([
        {id, matches: [origin], js: ['content.js'], allFrames: true, runAt: 'document_start'}]);
      await api.runtime.sendMessage({type: 'permissions_changed'});
      status.textContent = '网站授权完成。';
    }
  } catch (error) { status.textContent = error.message; }
};
document.getElementById('bind').onclick = async () => {
  const [tab] = await api.tabs.query({active: true, currentWindow: true});
  const result = await api.runtime.sendMessage({type: 'bind', tabId: tab.id});
  status.textContent = result.ok ? '已绑定当前网页。' : result.reason || '绑定失败';
};
refresh().catch(() => { status.textContent = '扩展正在重新连接，请稍后再试。'; });
