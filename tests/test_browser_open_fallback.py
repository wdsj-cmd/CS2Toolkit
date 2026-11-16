import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication
from app.logic.visual_handler import VisualHandler

APP = QApplication.instance() or QApplication([])


class BrowserNavigationFallbackTests(unittest.TestCase):
    def setUp(self):
        recording = patch('app.logic.visual_handler.record')
        recording.start()
        self.addCleanup(recording.stop)
        class Config:
            def get(self, key, default=None):
                return {'flash_enabled': False, 'boss_key_enabled': True, 'boss_key_action': 'pause'} if key == 'visual' else default
        self.handler = VisualHandler(Config())
        self.addCleanup(self.handler.cleanup)
        self.handler._boss_key_triggered = self.handler._boss_key_has_switched = True
        self.handler._boss_key_request = 5
        self.handler._extension_open_request = (5, 'https://www.bilibili.com/')
        self.handler.death_switch_guard.start({'provider': {'steamid': 'self'},
            'map': {'name': 'de_dust2', 'mode': 'competitive', 'phase': 'live'},
            'round': {'phase': 'live'}, 'player': {'steamid': 'self', 'team': 'CT', 'state': {'health': 0}},
            'allplayers': {'friend': {'team': 'CT', 'state': {'health': 100}}}})
        self.handler._activate_browser_window = Mock(return_value=False)
        self.handler._start_target_media = Mock()
        self.handler._restore_boss_key_context = Mock()

    def test_failed_extension_navigation_really_opens_url_then_starts_target_media(self):
        with patch('PySide6.QtGui.QDesktopServices.openUrl', return_value=True) as opened:
            self.handler._extension_open_failed('command_failed')
            self.handler._extension_open_failed('command_failed')
        opened.assert_called_once()
        self.assertEqual(opened.call_args.args[0].toString(), 'https://www.bilibili.com/')
        self.handler._start_target_media.assert_called_once_with(5, 'url', 'https://www.bilibili.com/')

    def test_existing_browser_window_is_restored_without_an_extra_tab(self):
        self.handler._activate_browser_window.return_value = True
        with patch('PySide6.QtGui.QDesktopServices.openUrl') as opened:
            self.handler._extension_open_failed('command_failed')
        opened.assert_not_called()
        self.handler._start_target_media.assert_called_once()

    def test_return_to_game_invalidates_navigation_fallback(self):
        self.handler._boss_key_request = 6
        self.handler._extension_open_failed('command_failed')
        self.handler._activate_browser_window.assert_not_called()

    def test_successful_navigation_disarms_later_disconnect_navigation(self):
        self.handler._extension_watch_ready()
        self.handler._extension_open_failed('disconnected')
        self.handler._activate_browser_window.assert_not_called()

    def test_round_end_and_fast_respawn_mode_still_prevent_fallback_navigation(self):
        for change in ({'map': {'phase': 'gameover'}}, {'map': {'mode': 'deathmatch'}}):
            with self.subTest(change=change):
                self.handler._extension_open_request = (5, 'https://www.bilibili.com/')
                self.handler.death_switch_guard.update(change)
                self.handler._extension_open_failed('command_failed')
        self.handler._activate_browser_window.assert_not_called()
        self.assertEqual(self.handler._restore_boss_key_context.call_count, 2)

    def test_failed_os_url_open_restores_game_instead_of_leaving_desktop(self):
        with patch('PySide6.QtGui.QDesktopServices.openUrl', return_value=False):
            self.handler._extension_open_failed('command_failed')
        self.handler._restore_boss_key_context.assert_called_once()
        self.handler._start_target_media.assert_not_called()
