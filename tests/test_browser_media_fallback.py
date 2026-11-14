import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch, AsyncMock

from PySide6.QtCore import QObject, Signal

from app.logic.target_media_controller import TargetMediaController, MediaTarget
from app.logic.visual_handler import VisualHandler


class Bridge(QObject):
    failed = Signal(str)
    def __init__(self):
        super().__init__()
        self.controls_media = True
        self.pause = Mock(return_value=True)
        self.release = Mock()


class FallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_connected_extension_prevents_native_guard_from_controlling_other_browser_tabs(self):
        bridge = Bridge()
        controller = TargetMediaController(browser_bridge=bridge, manager_factory=Mock())
        self.addCleanup(controller.close)
        controller._session_manager = Mock(side_effect=AssertionError('native selection must be skipped'))
        await controller._guard_late_media()
        controller.pause()
        bridge.pause.assert_called_once()

    async def test_disconnect_arms_the_native_pause_guard_again(self):
        bridge = Bridge()
        controller = TargetMediaController(browser_bridge=bridge)
        self.addCleanup(controller.close)
        controller._target_hint = MediaTarget('chrome.exe', url='https://www.bilibili.com/')
        controller._desired_playing = False
        with patch.object(controller, '_ensure_worker') as start:
            bridge.controls_media = False
            bridge.failed.emit('disconnected')
        start.assert_called_once()
        command, generation, target = controller._commands.get_nowait()
        self.assertEqual(command, 'pause')
        self.assertEqual(target.process_name, 'chrome.exe')

    async def test_extension_reconnect_during_native_lookup_cancels_native_pause(self):
        bridge = Bridge()
        bridge.controls_media = False
        pause = AsyncMock(return_value=True)
        async def properties():
            bridge.controls_media = True
            return SimpleNamespace(title='视频_哔哩哔哩_bilibili')
        session = SimpleNamespace(source_app_user_model_id='Chrome',
            try_get_media_properties_async=properties, try_pause_async=pause,
            get_playback_info=lambda: SimpleNamespace(playback_status=4))
        async def manager():
            return SimpleNamespace(get_sessions=lambda: [session])
        controller = TargetMediaController(browser_bridge=bridge, manager_factory=manager,
                                           foreground_check=lambda: True)
        self.addCleanup(controller.close)
        controller._desired_playing = False
        controller._target_hint = MediaTarget('chrome.exe', '视频_哔哩哔哩_bilibili', url='https://www.bilibili.com/')
        with patch('app.logic.target_media_controller.find_media_windows', return_value=[]):
            await controller._guard_late_media()
        pause.assert_not_awaited()

    async def test_extension_open_bypasses_browser_title_activation_and_global_url_open(self):
        bridge = SimpleNamespace(open_url=Mock(return_value=True), native_target=Mock(return_value=None))
        context = SimpleNamespace(
            _boss_key_request=0, _boss_key_triggered=True, _boss_key_has_switched=False,
            death_switch_guard=SimpleNamespace(decision=lambda: 'allow', last_teammate=None),
            boss_key_action='pause', browser_bridge=bridge, target_media=Mock(),
            signals=SimpleNamespace(minimize_game=Mock()),
            _activate_browser_window=Mock(side_effect=AssertionError('extension owns exact tab selection')))
        callbacks = []
        with patch('app.logic.visual_handler.QTimer.singleShot', side_effect=lambda delay, fn: callbacks.append(fn)), \
                patch('app.logic.visual_handler.default_media_target', return_value=None), \
                patch('PySide6.QtGui.QDesktopServices.openUrl') as open_url:
            VisualHandler._open_boss_key_target(context, 'url', 'https://www.bilibili.com/', 0)
            callbacks[0]()
        bridge.open_url.assert_called_once_with('https://www.bilibili.com/')
        open_url.assert_not_called()
