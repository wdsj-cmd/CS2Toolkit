import copy
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PySide6.QtCore import QUrl, QEvent
from PySide6.QtNetwork import QAbstractSocket
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtWidgets import QApplication, QWidget

from app.logic.browser_media_bridge import BrowserMediaBridge, permitted
from app.logic.browser_extension_install import ExtensionInstaller

APP = QApplication.instance() or QApplication([])


def pump(seconds=.02):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        APP.processEvents()
        APP.sendPostedEvents(None, QEvent.DeferredDelete)
        time.sleep(.002)


def until(predicate, seconds=1):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        pump(.005)
    if not predicate():
        raise AssertionError('Qt connection did not reach expected state')


class Config:
    def __init__(self, folder):
        self.work_dir = folder
        self.values = {'visual': {'boss_key_target': 'https://www.bilibili.com/'}}
    def get(self, key, default=None): return self.values.get(key, default)
    def set(self, key, value): self.values[key] = copy.deepcopy(value)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(self.temp.name)
        self.bridge = BrowserMediaBridge(self.config)
        self.clients = []
        self.assertGreater(self.bridge.port, 0)

    def tearDown(self):
        for client in self.clients: client.close()
        self.bridge.close()
        self.bridge.deleteLater()
        pump(.04)
        self.temp.cleanup()

    def client(self, token=None, origin=''):
        client = QWebSocket(origin)
        self.clients.append(client)
        messages = []
        client.textMessageReceived.connect(lambda text: messages.append(json.loads(text)))
        client.open(QUrl(f'ws://127.0.0.1:{self.bridge.port}'))
        until(lambda: client.state() == QAbstractSocket.ConnectedState)
        client.sendTextMessage(json.dumps({'type': 'hello', 'token': token or self.bridge.token,
            'browser': 'Chrome', 'process': 'chrome.exe', 'version': '0.1.0',
            'origins': ['*://*.bilibili.com/*']}))
        pump(.03)
        return client, messages

    def test_authentication_required_and_bad_secret_cannot_control(self):
        client, messages = self.client('x' * 43)
        until(lambda: client.state() == QAbstractSocket.UnconnectedState)
        self.assertFalse(self.bridge.open_url('https://www.bilibili.com/'))
        self.assertFalse(any(message['type'] == 'authenticated' for message in messages))

    def test_normal_web_page_origin_is_rejected_even_with_secret(self):
        client = QWebSocket('https://example.com')
        self.clients.append(client)
        client.open(QUrl(f'ws://127.0.0.1:{self.bridge.port}'))
        pump(.1)
        self.assertFalse(any(c['authenticated'] for c in self.bridge._clients.values()))

    def test_watch_pause_focus_release_and_resume_are_real_websocket_messages(self):
        client, messages = self.client()
        self.assertTrue(self.bridge.open_url('https://www.bilibili.com/'))
        until(lambda: any(m.get('open') for m in messages))
        watch = next(m for m in messages if m.get('open'))
        client.sendTextMessage(json.dumps({'type': 'ack', 'seq': watch['seq'], 'handled': True}))
        pump()
        self.bridge.set_game_focus(True)
        self.assertTrue(self.bridge.pause())
        until(lambda: messages[-1].get('mode') == 'pause')
        self.bridge.set_game_focus(False)
        until(lambda: messages[-1].get('mode') == 'free')
        self.assertTrue(self.bridge.open_url('https://www.bilibili.com/'))
        until(lambda: messages[-1].get('mode') == 'watch')
        self.assertEqual(messages[-1]['epoch'], watch['epoch'])

    def test_other_sites_require_extension_permission(self):
        self.client()
        self.assertFalse(self.bridge.open_url('https://www.douyin.com/'))
        self.assertFalse(self.bridge.open_url('javascript:alert(1)'))

    def test_native_fallback_hint_uses_the_bound_browsers_process(self):
        self.client()
        self.bridge.open_url('https://www.bilibili.com/')
        self.assertEqual(self.bridge.native_target().process_name, 'chrome.exe')

    def test_install_button_is_in_death_switch_dialog_and_uses_the_shared_bridge(self):
        from app.ui.pages.visual_page import VisualConfigDialog
        self.config.browser_bridge = self.bridge
        parent = QWidget()
        dialog = VisualConfigDialog(self.config, 'boss_key', parent)
        with patch.object(self.bridge, 'install') as install:
            dialog.extension_install_button.click()
            install.assert_called_once_with(dialog)
        self.client()
        self.assertIn('Chrome', dialog.extension_status.text())
        dialog.close(); parent.close()

    def test_disconnect_releases_ownership_and_notifies_native_fallback(self):
        client, _ = self.client()
        errors = []
        self.bridge.failed.connect(errors.append)
        self.bridge.open_url('https://www.bilibili.com/')
        client.close()
        until(lambda: not self.bridge.controls_media)
        self.assertIn('disconnected', errors)

    def test_stale_negative_ack_cannot_cancel_the_new_pause(self):
        client, _ = self.client()
        self.bridge.open_url('https://www.bilibili.com/')
        old = self.bridge._seq
        self.bridge.pause()
        client.sendTextMessage(json.dumps({'type': 'ack', 'seq': old, 'handled': False}))
        pump()
        self.assertTrue(self.bridge.controls_media)

    def test_unresponsive_extension_times_out_to_native_control(self):
        self.client()
        errors = []
        self.bridge.failed.connect(errors.append)
        self.bridge.open_url('https://www.bilibili.com/')
        until(lambda: not self.bridge.controls_media, seconds=3.2)
        self.assertEqual(errors, ['no_acknowledgement'])

    def test_install_stages_both_browser_variants_without_pairing_secret(self):
        installer = ExtensionInstaller(self.bridge)
        installer.stage()
        chromium = json.loads((installer.destination/'chromium/manifest.json').read_text(encoding='utf-8'))
        firefox = json.loads((installer.destination/'firefox/manifest.json').read_text(encoding='utf-8'))
        self.assertIn('service_worker', chromium['background'])
        self.assertIn('scripts', firefox['background'])
        for path in installer.destination.rglob('*'):
            if path.is_file(): self.assertNotIn(self.bridge.token, path.read_text(encoding='utf-8'))

    def test_cancel_update_neither_overwrites_files_nor_opens_browser(self):
        installer = ExtensionInstaller(self.bridge)
        installer.stage()
        marker = installer.destination/'chromium/background.js'
        marker.write_text('unchanged', encoding='utf-8')
        with patch('qfluentwidgets.MessageBox') as dialog, \
                patch('app.logic.browser_extension_install.QDesktopServices.openUrl') as opened:
            dialog.return_value.exec.return_value = 0
            self.assertFalse(installer.open(QWidget()))
        opened.assert_not_called()
        self.assertEqual(marker.read_text(encoding='utf-8'), 'unchanged')

    def test_confirm_update_replaces_managed_files_then_opens_pairing_in_default_browser(self):
        self.client()
        installer = ExtensionInstaller(self.bridge)
        self.addCleanup(installer.close)
        installer.stage()
        marker = installer.destination/'chromium/background.js'
        marker.write_text('old version', encoding='utf-8')
        with patch('qfluentwidgets.MessageBox') as dialog, \
                patch('app.logic.browser_extension_install.QDesktopServices.openUrl', return_value=True) as opened, \
                patch.object(self.bridge, 'reload_extensions') as reload:
            dialog.return_value.exec.return_value = 1
            self.assertTrue(installer.open(QWidget()))
        self.assertNotEqual(marker.read_text(encoding='utf-8'), 'old version')
        reload.assert_called_once()
        url = opened.call_args.args[0]
        self.assertEqual(url.host(), '127.0.0.1')
        self.assertTrue(url.fragment().startswith('pair='))
        self.assertEqual(url.query(), '')

    def test_install_guide_is_loopback_only_and_does_not_serve_secret(self):
        installer = ExtensionInstaller(self.bridge)
        self.addCleanup(installer.close)
        installer.stage()
        installer._start_server()
        url = f'http://127.0.0.1:{installer.server.server_port}/install'
        with urlopen(url) as response:
            page = response.read().decode('utf-8')
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertNotIn(self.bridge.token, page)
        self.assertIn('chrome://extensions', page)
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(url, headers={'Host': 'evil.example'}))
        self.assertEqual(error.exception.code, 403)


class PermissionTests(unittest.TestCase):
    def test_host_boundaries_and_scheme_are_respected(self):
        origins = ['*://*.bilibili.com/*']
        for url in ('https://bilibili.com/video/1', 'https://player.bilibili.com/x'):
            self.assertTrue(permitted(url, origins))
        for url in ('https://evilbilibili.com/', 'https://bilibili.com.evil.com/', 'file:///video'):
            self.assertFalse(permitted(url, origins))
