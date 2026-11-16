import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication
from app.logic.death_switch_guard import DeathSwitchGuard
from app.logic.visual_handler import VisualHandler

APP = QApplication.instance() or QApplication([])


def state(mode='competitive', health=0, name='de_dust2'):
    return {'provider': {'steamid': 'self'}, 'map': {'name': name, 'round': 2, 'phase': 'live', 'mode': mode},
            'round': {'phase': 'live'}, 'player': {'steamid': 'self', 'team': 'CT', 'state': {'health': health}},
            'allplayers': {'friend': {'team': 'CT', 'state': {'health': 100}}}}


class FastRespawnGuardTests(unittest.TestCase):
    def setUp(self):
        self.now = 10
        self.guard = DeathSwitchGuard(clock=lambda: self.now)

    def test_deathmatch_and_arms_race_block_despite_surviving_teammates(self):
        for mode in ('deathmatch', 'gungameprogressive'):
            with self.subTest(mode=mode):
                self.guard.start(state(mode))
                self.assertEqual(self.guard.decision(), 'skip')
                self.assertEqual(self.guard.reason, 'fast_respawn_mode')

    def test_missing_roster_grace_does_not_allow_rapid_respawn_modes(self):
        for mode in ('deathmatch', 'gungameprogressive'):
            packet = state(mode)
            packet.pop('allplayers')
            self.guard.start(packet)
            self.now += .9
            self.assertEqual(self.guard.decision(), 'skip')

    def test_mode_seen_before_death_survives_sparse_death_packet(self):
        self.guard.update(state('gungameprogressive', health=100))
        packet = state()
        packet.pop('map')
        self.guard.start(packet)
        self.assertEqual(self.guard.mode, 'gungameprogressive')
        self.assertEqual(self.guard.decision(), 'skip')

    def test_return_to_competitive_on_same_map_reenables_next_death(self):
        self.guard.start(state('deathmatch'))
        self.guard.start(state('competitive'))
        self.assertEqual(self.guard.decision(), 'allow')

    def test_map_change_clears_stale_mode_when_new_mode_is_not_supplied(self):
        self.guard.update(state('deathmatch', health=100))
        packet = state(name='de_mirage')
        packet['map'].pop('mode')
        self.guard.start(packet)
        self.assertEqual(self.guard.mode, '')
        self.assertEqual(self.guard.decision(), 'allow')

    def test_round_based_modes_and_unknown_mode_keep_existing_switching_behavior(self):
        for mode in ('competitive', 'casual', 'scrimcomp2v2', '', 'community_unknown'):
            with self.subTest(mode=mode):
                self.guard.start(state(mode))
                self.assertEqual(self.guard.decision(), 'allow')

    def test_mode_string_is_normalized(self):
        self.guard.start(state(' DEATHMATCH '))
        self.assertEqual(self.guard.decision(), 'skip')


class FastRespawnIntegrationTests(unittest.TestCase):
    def setUp(self):
        recording = patch('app.logic.visual_handler.record')
        recording.start()
        self.addCleanup(recording.stop)
        class Config:
            values = {'visual': {'boss_key_enabled': True, 'boss_key_action': 'pause', 'flash_enabled': False}}
            def get(self, key, default=None): return self.values.get(key, default)
        self.handler = VisualHandler(Config())
        self.addCleanup(self.handler.cleanup)
        self.requests = []
        self.handler.signals.open_boss_key_target.disconnect()
        self.handler.signals.open_boss_key_target.connect(lambda *args: self.requests.append(args))

    def test_repeated_rapid_respawns_never_schedule_browser_switch(self):
        for mode in ('deathmatch', 'gungameprogressive'):
            for health in (100, 0, 100, 0, 100, 0):
                self.handler.process_gsi(state(mode, health))
        self.assertEqual(self.requests, [])
        self.assertFalse(self.handler._boss_key_triggered)

    def test_map_only_mode_update_blocks_a_later_sparse_death(self):
        self.handler.process_gsi({'map': {'name': 'de_dust2', 'mode': 'deathmatch'}})
        packet = state()
        packet.pop('map')
        self.handler.process_gsi(packet)
        self.assertEqual(self.requests, [])

    def test_late_mode_detection_cancels_queued_native_and_extension_switch(self):
        self.handler.process_gsi(state())
        self.handler.browser_bridge = Mock()
        self.handler.signals.minimize_game.disconnect()
        minimize = Mock()
        self.handler.signals.minimize_game.connect(minimize)
        callbacks = []
        with patch('app.logic.visual_handler.QTimer.singleShot', side_effect=lambda delay, fn: callbacks.append(fn)), \
                patch('PySide6.QtGui.QDesktopServices.openUrl') as opened:
            self.handler._open_boss_key_target('url', 'https://www.bilibili.com/', 2000)
            self.handler.process_gsi({'map': {'mode': 'gungameprogressive'}})
            callbacks[0]()
        minimize.assert_not_called()
        opened.assert_not_called()
        self.handler.browser_bridge.open_url.assert_not_called()

    def test_competitive_death_still_schedules_switch_after_leaving_deathmatch(self):
        self.handler.process_gsi(state('deathmatch'))
        self.handler.process_gsi(state('competitive', health=100))
        self.handler.process_gsi(state('competitive'))
        self.assertEqual(len(self.requests), 1)
