import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from app.logic.config_manager import ConfigManager
from app.logic.gsi_manager import GSIManager
from app.main_window import CS2Tool

APP = QApplication.instance() or QApplication([])


class GsiConfigSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game = Path(self.temp.name)
        self.directory = self.game / 'game/csgo/cfg'
        self.directory.mkdir(parents=True)
        self.path = self.directory / 'gamestate_integration_cs2toolkit.cfg'
        self.manager = GSIManager(str(self.game))

    def write_old_config(self):
        content = self.manager.get_gsi_cfg_content().replace('"allplayers_match_stats" "1"',
                                                            '"allplayers_match_stats" "0"')
        self.path.write_text(content, encoding='utf-8')
        return self.path.read_bytes()

    def test_missing_config_is_created_from_current_template(self):
        ok, _ = self.manager.create_gsi_cfg()
        self.assertTrue(ok)
        self.assertEqual(self.path.read_text(encoding='utf-8'), self.manager.get_gsi_cfg_content())
        self.assertEqual(getattr(self.manager, 'last_cfg_action', None), 'created')

    def test_matching_config_is_not_rewritten(self):
        self.path.write_text(self.manager.get_gsi_cfg_content(), encoding='utf-8')
        os.utime(self.path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        before = self.path.stat().st_mtime_ns
        ok, _ = self.manager.create_gsi_cfg()
        self.assertTrue(ok)
        self.assertEqual(self.path.stat().st_mtime_ns, before)
        self.assertEqual(getattr(self.manager, 'last_cfg_action', None), 'unchanged')

    def test_bom_and_windows_newlines_do_not_force_rewrite(self):
        content = self.manager.get_gsi_cfg_content().replace('\n', '\r\n')
        self.path.write_bytes(b'\xef\xbb\xbf' + content.encode())
        before = self.path.read_bytes()
        self.assertTrue(self.manager.create_gsi_cfg()[0])
        self.assertEqual(self.path.read_bytes(), before)

    def test_changed_subscription_is_automatically_upgraded(self):
        self.write_old_config()
        self.assertTrue(self.manager.create_gsi_cfg()[0])
        self.assertEqual(self.path.read_text(encoding='utf-8'), self.manager.get_gsi_cfg_content())
        self.assertEqual(getattr(self.manager, 'last_cfg_action', None), 'updated')

    def test_actual_listener_port_is_kept_during_upgrade(self):
        self.write_old_config()
        self.manager.current_port = 3168
        self.assertTrue(self.manager.create_gsi_cfg()[0])
        self.assertIn('"uri" "http://127.0.0.1:3168"', self.path.read_text(encoding='utf-8'))
        self.assertEqual(self.manager.current_port, 3168)

    def test_malformed_encoding_is_replaced_with_valid_current_config(self):
        self.path.write_bytes(b'\xff\xfe\x00\x81broken')
        self.assertTrue(self.manager.create_gsi_cfg()[0])
        self.assertEqual(self.path.read_text(encoding='utf-8'), self.manager.get_gsi_cfg_content())
        self.assertEqual(getattr(self.manager, 'last_cfg_action', None), 'updated')

    def test_failed_atomic_replace_preserves_old_file_and_removes_temporary_file(self):
        before = self.write_old_config()
        with patch('os.replace', side_effect=PermissionError('write denied')):
            ok, message = self.manager.create_gsi_cfg()
        self.assertFalse(ok)
        self.assertIn('write denied', message)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.directory.iterdir()), [self.path])
        self.assertEqual(getattr(self.manager, 'last_cfg_action', None), 'error')

    def test_failed_write_preserves_old_file(self):
        before = self.write_old_config()
        with patch('tempfile.NamedTemporaryFile', side_effect=PermissionError('write denied')):
            ok, _ = self.manager.create_gsi_cfg()
        self.assertFalse(ok)
        self.assertEqual(self.path.read_bytes(), before)

    def test_other_tools_gsi_configs_are_left_untouched(self):
        other = self.directory / 'gamestate_integration_other.cfg'
        other.write_bytes(b'other tool configuration')
        self.write_old_config()
        self.assertTrue(self.manager.create_gsi_cfg()[0])
        self.assertEqual(other.read_bytes(), b'other tool configuration')


class GsiAutoSyncIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        directory = self.temp.name
        class IsolatedConfig(ConfigManager):
            def _resolve_work_dir(self, directory_name): return directory
            def _migrate_legacy_work_dir(self): pass
        self.notices, self.events = [], []
        patches = [
            patch('app.main_window.ConfigManager', IsolatedConfig),
            patch.object(CS2Tool, '_deferred_startup_tasks'),
            patch.object(CS2Tool, '_repair_current_font_if_needed'),
            patch('app.ui.pages.home_page.HomePage._fetch_announcement'),
            patch('app.ui.pages.about_page.AboutPage._fetch_donation_content'),
            patch('app.main_window.record', side_effect=lambda event, **details: self.events.append((event, details))),
            patch.object(CS2Tool, 'show_warning', side_effect=lambda title, message, **kw: self.notices.append((title, message))),
            patch.object(CS2Tool, 'show_success'),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.window = CS2Tool()
        self.addCleanup(self.close_window)
        self.game = Path(directory) / 'CS2'
        exe = self.game / 'game/bin/win64/cs2.exe'
        exe.parent.mkdir(parents=True)
        exe.touch()
        self.path = self.game / 'game/csgo/cfg/gamestate_integration_cs2toolkit.cfg'
        self.path.parent.mkdir(parents=True)
        self.window.config_manager.set('steam_path', str(self.game))
        self.window.steam_path = str(self.game)
        self.window.gsi_manager.set_cs2_path(str(self.game))

    def close_window(self):
        self.window.music_box.cleanup()
        self.window.browser_bridge.close()
        self.window.go_pet_manager.set_enabled(False)
        self.window._force_quit = True
        self.window.close()
        APP.processEvents()

    def test_startup_upgrades_old_config_and_prompts_running_game_only_once(self):
        manager = self.window.gsi_manager
        self.path.write_text(manager.get_gsi_cfg_content().replace('"allplayers_match_stats" "1"',
                                                                  '"allplayers_match_stats" "0"'), encoding='utf-8')
        with patch('app.main_window.LaunchManager.is_cs2_running', return_value=True):
            self.window._perform_steam_detection()
            modified = self.path.stat().st_mtime_ns
            self.window._perform_steam_detection()
        self.assertEqual(self.path.read_text(encoding='utf-8'), manager.get_gsi_cfg_content())
        self.assertEqual(self.path.stat().st_mtime_ns, modified)
        self.assertEqual(len(self.notices), 1)
        self.assertIn('重启', self.notices[0][1])
        sync = [details for event, details in self.events if event == 'gsi_config_sync']
        self.assertEqual([item['action'] for item in sync], ['updated'])

    def test_listener_restart_syncs_real_port_without_prompt_when_game_is_closed(self):
        manager = self.window.gsi_manager
        self.path.write_text(manager.get_gsi_cfg_content(), encoding='utf-8')
        manager.current_port = 3168
        with patch('app.main_window.LaunchManager.is_cs2_running', return_value=False):
            self.window.handle_gsi_data_on_ui_thread({'success': 'server_started', 'port': 3168})
        self.assertIn('"uri" "http://127.0.0.1:3168"', self.path.read_text(encoding='utf-8'))
        self.assertEqual(self.window.config_manager.get('gsi_port'), 3168)
        self.assertEqual(self.notices, [])
        sync = [details for event, details in self.events if event == 'gsi_config_sync']
        self.assertEqual([item['action'] for item in sync], ['updated'])

    def test_startup_update_failure_is_visible_and_preserves_existing_config(self):
        self.path.write_bytes(b'old game configuration')
        with patch('os.replace', side_effect=PermissionError('write denied')):
            self.window._perform_steam_detection()
        self.assertEqual(self.path.read_bytes(), b'old game configuration')
        self.assertEqual(len(self.notices), 1)
        self.assertIn('write denied', self.notices[0][1])
        sync = [details for event, details in self.events if event == 'gsi_config_sync']
        self.assertEqual([item['action'] for item in sync], ['error'])


if __name__ == '__main__':
    unittest.main()
