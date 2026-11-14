"""Authenticated loopback connection to the optional browser extension."""
import hmac
import json
import secrets
import threading
import time
from urllib.parse import urlparse

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QHostAddress
from PySide6.QtWebSockets import QWebSocketServer

from .flash_diagnostics import record


def website(url):
    parsed = urlparse(url)
    return (parsed.hostname or '').lower() if parsed.scheme in ('http', 'https') else ''


def permitted(url, origins):
    parsed = urlparse(url)
    if not website(url):
        return False
    for pattern in origins:
        if pattern == '<all_urls>':
            return True
        scheme, separator, host = pattern.partition('://')
        if not separator or scheme not in ('*', parsed.scheme):
            continue
        host = host.split('/')[0]
        actual = parsed.hostname.lower()
        if host == '*' or host == actual or (host.startswith('*.') and
                (actual == host[2:] or actual.endswith('.' + host[2:]))):
            return True
    return False


class BrowserMediaBridge(QObject):
    changed = Signal()
    failed = Signal(str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        saved = config.get('browser_extension', {})
        saved = saved if isinstance(saved, dict) else {}
        token = saved.get('token')
        self.token = token if isinstance(token, str) and 32 <= len(token) <= 128 else secrets.token_urlsafe(32)
        self.server = QWebSocketServer('CS2Toolkit browser media', QWebSocketServer.NonSecureMode, self)
        port = saved.get('port', 0)
        if not isinstance(port, int) or not 0 <= port <= 65535:
            port = 0
        if not self.server.listen(QHostAddress.LocalHost, port):
            self.server.listen(QHostAddress.LocalHost, 0)
        self.port = self.server.serverPort()
        if self.port:
            config.set('browser_extension', {'token': self.token, 'port': self.port})
        self.server.newConnection.connect(self._connected)
        self._clients = {}
        self._lock = threading.RLock()
        self._active = None
        self._resume_client = ''
        self._owned = False
        self._seq = 0
        self._epoch = secrets.token_hex(12)
        self._url = config.get('visual', {}).get('boss_key_target', '')
        self._mode = 'free'
        self._want_pause = False
        self._game_focused = False
        self._closed = False
        self._installer = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._expire)
        self.timer.start(5000)

    def _connected(self):
        socket = self.server.nextPendingConnection()
        origin = socket.origin()
        if self._closed or len(self._clients) >= 8 or (origin and not origin.startswith(
                ('chrome-extension://', 'moz-extension://'))):
            socket.close()
            socket.deleteLater()
            return
        socket.setMaxAllowedIncomingMessageSize(16384)
        with self._lock:
            self._clients[socket] = {'authenticated': False, 'seen': time.monotonic()}
        socket.textMessageReceived.connect(lambda text: self._message(socket, text))
        socket.disconnected.connect(lambda: self._disconnected(socket))
        QTimer.singleShot(3000, self, lambda: socket.close() if socket in self._clients
                          and not self._clients[socket]['authenticated'] else None)

    def _message(self, socket, text):
        try:
            message = json.loads(text)
            if not isinstance(message, dict):
                raise ValueError()
            client = self._clients.get(socket)
            if client is None:
                return
            if not client['authenticated']:
                token = message.get('token', '')
                if message.get('type') != 'hello' or not isinstance(token, str) or not hmac.compare_digest(token, self.token):
                    socket.close()
                    return
                origins = message.get('origins', [])
                if not isinstance(origins, list) or len(origins) > 128 or not all(isinstance(p, str) for p in origins):
                    raise ValueError()
                client.update(authenticated=True, browser=str(message.get('browser', '浏览器'))[:40],
                              version=str(message.get('version', ''))[:24], origins=origins,
                              process=str(message.get('process', ''))[:40],
                              identity=str(message.get('client_id', ''))[:64])
                self._send(socket, {'type': 'authenticated'})
                if (not self.controls_media and self._active is None and self._want_pause and self._game_focused
                        and self._resume_client and client['identity'] == self._resume_client
                        and permitted(self._url, origins)):
                    self._active, self._owned, self._mode = socket, True, 'pause'
                    self._seq += 1
                    self._send(socket, self._snapshot())
                    self._expect_ack(self._seq, socket)
                else:
                    self._send(socket, self._snapshot(open_tab=False, mode='free'))
                record('boss_extension_connected', browser=client['browser'], version=client['version'])
                self.changed.emit()
            client['seen'] = time.monotonic()
            if message.get('type') == 'ping':
                self._send(socket, {'type': 'pong'})
            elif message.get('type') == 'permissions':
                origins = message.get('origins', [])
                if isinstance(origins, list) and len(origins) <= 128 and all(isinstance(p, str) for p in origins):
                    client['origins'] = origins
                    self.changed.emit()
            elif message.get('type') == 'ack' and socket is self._active and message.get('seq') == self._seq:
                client['ack_seq'] = self._seq
                record('boss_extension_result', mode=self._mode, handled=bool(message.get('handled')),
                       reason=str(message.get('reason', ''))[:64], media_count=message.get('media_count', 0))
                if not message.get('handled'):
                    self._lose_control(str(message.get('reason', 'unavailable'))[:64])
        except (ValueError, TypeError, AttributeError):
            socket.close()

    def _send(self, socket, message):
        if socket in self._clients:
            socket.sendTextMessage(json.dumps(message, ensure_ascii=False))

    def _snapshot(self, open_tab=False, mode=None):
        return {'type': 'state', 'seq': self._seq, 'url': self._url,
                'mode': mode or self._mode, 'open': open_tab, 'epoch': self._epoch}

    def _disconnected(self, socket):
        with self._lock:
            self._clients.pop(socket, None)
        if socket is self._active:
            self._lose_control('disconnected')
            self._active = None
        socket.deleteLater()
        self.changed.emit()

    def _lose_control(self, reason):
        with self._lock:
            was_owned, self._owned = self._owned, False
        if was_owned:
            self._set_mode('free')
            record('boss_extension_fallback', reason=reason)
            self.failed.emit(reason)

    def _expire(self):
        for socket, client in list(self._clients.items()):
            if time.monotonic() - client['seen'] > 45:
                socket.close()

    @property
    def controls_media(self):
        with self._lock:
            return self._owned and self._active in self._clients

    def description(self):
        clients = [c for c in self._clients.values() if c['authenticated']]
        return ('扩展已连接 · ' + '、'.join(c['browser'] + ' ' + c['version'] for c in clients)
                if clients else '扩展未连接，使用系统媒体控制')

    def open_url(self, url):
        if not website(url) or self._closed:
            return False
        candidates = [s for s, c in self._clients.items() if c['authenticated'] and permitted(url, c['origins'])]
        if not candidates:
            return False
        from .target_media_controller import default_media_target
        default = default_media_target('url', url)
        preferred = [s for s in candidates if default is not None
                     and self._clients[s].get('process') == default.process_name]
        socket = self._active if self._active in candidates else (preferred or candidates)[-1]
        if self._active is not None and self._active is not socket:
            self._send(self._active, self._snapshot(mode='free'))
        self._active, self._url = socket, url
        self._resume_client = self._clients[socket].get('identity', '')
        self._owned = True
        self._want_pause = False
        self._mode = 'watch'
        self._seq += 1
        seq = self._seq
        self._send(socket, self._snapshot(open_tab=True))
        # A responding extension acknowledges even when video has not loaded.
        self._expect_ack(seq, socket)
        return True

    def configure_url(self, url):
        """Publish a changed setting before the site has permission to open."""
        url = url.strip()
        if '://' not in url:
            url = 'https://' + url
        if not website(url) or url == self._url:
            return
        self.release()
        self._url = url
        self._seq += 1
        for socket, client in list(self._clients.items()):
            if client['authenticated']:
                self._send(socket, self._snapshot(mode='free'))

    def _expect_ack(self, seq, socket):
        client = self._clients.get(socket)
        if client is None:
            return
        client['ack_seq'] = None
        QTimer.singleShot(2500, self, lambda: self._lose_control('no_acknowledgement')
                          if self._seq == seq and self.controls_media
                          and client.get('ack_seq') != seq else None)

    def pause(self):
        if not self.controls_media:
            return False
        self._want_pause = True
        self._set_mode('pause')
        return True

    def native_target(self):
        from .target_media_controller import BROWSERS, MediaTarget
        client = self._clients.get(self._active, {})
        process = client.get('process', '')
        return MediaTarget(process, url=self._url) if process in BROWSERS else None

    def set_game_focus(self, focused):
        if focused == self._game_focused:
            return
        self._game_focused = focused
        if self.controls_media and self._want_pause:
            self._set_mode('pause' if focused else 'free')

    def _set_mode(self, mode):
        if mode == self._mode:
            return
        self._mode = mode
        self._seq += 1
        self._send(self._active, self._snapshot())
        if self.controls_media:
            self._expect_ack(self._seq, self._active)

    def release(self):
        if self.controls_media:
            self._set_mode('free')
        with self._lock:
            self._owned = False
            self._want_pause = False

    def install(self, parent):
        from .browser_extension_install import ExtensionInstaller
        if self._installer is None:
            self._installer = ExtensionInstaller(self)
        self._installer.open(parent)

    def reload_extensions(self):
        for socket, client in list(self._clients.items()):
            if client['authenticated']:
                self._send(socket, {'type': 'reload'})

    def close(self):
        if self._closed:
            return
        self.release()
        self._closed = True
        self.timer.stop()
        for socket in list(self._clients):
            socket.close()
        self.server.close()
        if self._installer is not None:
            self._installer.close()
