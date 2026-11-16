"""Stage the bundled extension and open an honest, local installation guide."""
import base64
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import threading
from urllib.parse import urlsplit

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices


class ExtensionInstaller:
    def __init__(self, bridge):
        self.bridge = bridge
        self.destination = Path(bridge.config.work_dir) / 'browser_extension'
        self.source = Path(__file__).resolve().parents[1] / 'browser_extension'
        self.server = None

    def stage(self):
        files = ('background.js', 'protocol.js', 'content.js', 'pair.js', 'popup.html', 'popup.js')
        for variant, manifest in (('chromium', 'manifest.json'), ('firefox', 'manifest.firefox.json')):
            target = self.destination / variant
            target.mkdir(parents=True, exist_ok=True)
            # Copy only packaged files; retain the stable directory used by an
            # already-loaded unpacked extension. No browser profile is edited.
            for name in files:
                shutil.copy2(self.source / name, target / name)
            shutil.copy2(self.source / manifest, target / 'manifest.json')

    def open(self, parent):
        from qfluentwidgets import MessageBox, InfoBar
        exists = (self.destination / 'chromium/manifest.json').is_file()
        connected = any(c['authenticated'] for c in self.bridge._clients.values())
        if exists or connected:
            dialog = MessageBox('更新浏览器扩展', '检测到已有扩展文件或已连接的扩展。是否覆盖工具管理的本地扩展文件，并重新连接？\n从商店或其他目录安装的扩展，请在浏览器中更新或重新加载下方目录。', parent)
            dialog.yesButton.setText('覆盖更新')
            dialog.cancelButton.setText('取消')
            if not dialog.exec():
                return False
        try:
            if not self.bridge.port:
                raise OSError('本地扩展连接服务未启动')
            self.stage()
            self._start_server()
            payload = base64.urlsafe_b64encode(json.dumps({
                'url': f'ws://127.0.0.1:{self.bridge.port}', 'token': self.bridge.token
            }).encode()).decode().rstrip('=')
            url = f'http://127.0.0.1:{self.server.server_port}/install#pair={payload}'
            if connected:
                self.bridge.reload_extensions()
            opened = QDesktopServices.openUrl(QUrl(url))
            if not opened:
                raise OSError('无法打开默认浏览器')
            return True
        except OSError as error:
            InfoBar.error('扩展安装引导未打开', str(error), parent=parent)
            return False

    def _start_server(self):
        if self.server is not None:
            return
        installer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
                    self.send_error(403)
                    return
                route = urlsplit(self.path).path
                if route != '/install':
                    self.send_error(404)
                    return
                body = installer.page().encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Frame-Options', 'DENY')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, name='ExtensionInstallGuide', daemon=True).start()

    def page(self):
        chromium = html.escape(str(self.destination / 'chromium'))
        firefox = html.escape(str(self.destination / 'firefox/manifest.json'))
        # Pairing credentials are in the URL fragment, never HTTP requests/logs.
        return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="cs2toolkit-extension" content="1"><meta name="viewport" content="width=device-width">
<title>CS2 工具箱 · 浏览器扩展安装</title><style>
body{{font:16px/1.8 system-ui,"Microsoft YaHei";background:#f3f6fb;color:#20304a;margin:0}}
main{{max-width:760px;margin:48px auto;padding:36px;background:white;border-radius:20px}}
h1{{line-height:1.35}}code{{display:block;padding:14px;background:#eef3fa;overflow-wrap:anywhere}}
button{{background:#087acb;color:white;border:0;padding:12px 20px;border-radius:8px;cursor:pointer}}
.note{{color:#5d6f87}}#state{{font-weight:600;color:#087acb}}
</style><main><h1>获得更稳定的网页切换体验</h1>
<p>扩展按网页标签控制播放和暂停，支持 B 站打开新视频。未安装或断开时，工具仍可使用系统媒体控制。</p>
<p id="state">扩展尚未连接。加载后点击下方“连接扩展”。</p>
<h2>Chrome、Edge、Brave、Opera 等浏览器</h2>
<ol><li>在地址栏打开扩展管理页：Chrome 为 <b>chrome://extensions</b>，Edge 为 <b>edge://extensions</b>，其他 Chromium 浏览器使用对应的扩展管理页。</li>
<li>打开“开发者模式”，点击“加载已解压的扩展”，选择以下文件夹：</li></ol>
<code>{chromium}</code>
<h2>Firefox 测试加载</h2><p>打开 <b>about:debugging#/runtime/this-firefox</b>，点击“临时载入附加组件”，选择：</p>
<code>{firefox}</code><p class="note">Firefox 临时扩展在重启浏览器后需要重新加载。正式分发需 Mozilla 签名。</p>
<p><button onclick="location.reload()">已加载，连接扩展</button></p>
<p>连接后可点击扩展图标，查看连接状态、授权其他网站或绑定当前网页。</p>
<p class="note">当前为未上架测试版，需由你在浏览器中确认加载；打开此页面不会自动完成安装。已有开发版可由工具覆盖本地文件并重新连接。</p>
</main><script>addEventListener('cs2toolkit-paired',e=>{{document.getElementById('state').textContent=e.detail?'扩展已连接，可以返回工具或打开目标网页。':'连接失败，请保持工具运行，再点击连接。';}});</script></html>'''

    def close(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
