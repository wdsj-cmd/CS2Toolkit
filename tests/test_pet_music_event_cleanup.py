import unittest
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch
from PySide6.QtWidgets import QWidget

from app.logic.go_pet_manager import GoPetManager
from app.ui.pages.go_pet_page import GoPetPage, PetEventDialog, PET_EVENT_OPTIONS
import test_music_finish_before_switch as base

RETIRED = {'death', 'bomb', 'win', 'loss', 'mvp', 'freeze'}
RETAINED = {'normal', 'low_health', 'flashed', 'kill', 'rich', 'poor'}


class PetMusicEventCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.config = base.Config(self.temp.name)
        self.asset = Path(self.temp.name)/'keep.png'; self.asset.write_bytes(b'preserved test asset')
        self.events = [{'type': key, 'image': str(self.asset), 'sound': ''} for key in sorted(RETIRED | RETAINED)]
        self.config.set('go_pet', {'enabled': True, 'events': self.events, 'size': 200})

    def manager(self):
        with patch('app.logic.go_pet_manager.GoPetOverlay'), patch('app.logic.go_pet_manager.GoPetCaptureWindow'), \
                patch('app.logic.go_pet_manager.SoundPlayer'):
            manager = GoPetManager(self.config)
        self.addCleanup(lambda: manager.reset_timer.stop())
        return manager

    def test_menu_only_contains_retained_events(self):
        self.assertEqual({key for key, _ in PET_EVENT_OPTIONS}, RETAINED)
        parent = QWidget(); self.addCleanup(parent.close)
        dialog = PetEventDialog(parent)
        self.assertEqual({key for key, _ in dialog.event_types}, RETAINED)
        dialog.close(); dialog.deleteLater()

    def test_old_config_is_cleaned_without_deleting_its_media(self):
        page = GoPetPage(self.config)
        self.addCleanup(page.close)
        saved = self.config.get('go_pet')['events']
        self.assertEqual({event['type'] for event in saved}, RETAINED)
        self.assertTrue(self.asset.exists())
        self.assertEqual(len(page.event_widgets), len(RETAINED))

    def test_runtime_filters_old_rules_and_rejects_direct_retired_triggers(self):
        manager = self.manager()
        self.assertEqual(set(manager.events_config), RETAINED)
        self.assertEqual(set(manager.priority_map), RETAINED)
        manager.overlay.update_pet.reset_mock(); manager.sound_player.play_sound.reset_mock()
        for event in RETIRED: manager._trigger_event(event, transient=True)
        manager.overlay.update_pet.assert_not_called()
        manager.sound_player.play_sound.assert_not_called()

    def test_death_bomb_round_result_and_preparation_never_dispatch_retired_events(self):
        manager = self.manager()
        with patch.object(manager, '_trigger_event', wraps=manager._trigger_event) as triggered:
            for phase, health, bomb, mvps in (('live', 100, '', 0), ('live', 0, 'planted', 0),
                                              ('over', 0, 'exploded', 1), ('freezetime', 100, '', 1)):
                manager.process_gsi({'provider': {'steamid': 'self'}, 'round': {'phase': phase, 'bomb': bomb, 'win_team': 'T'},
                    'player': {'steamid': 'self', 'team': 'T', 'activity': 'playing',
                        'state': {'health': health, 'flashed': 0}, 'match_stats': {'kills': 0, 'mvps': mvps}}})
        self.assertFalse({call.args[0] for call in triggered.call_args_list} & RETIRED)

    def test_retained_kill_flash_and_economy_rules_still_dispatch(self):
        manager = self.manager()
        with patch.object(manager, '_trigger_event', wraps=manager._trigger_event) as triggered:
            for phase, flashed, kills, money in (('live', 0, 0, 5000), ('live', 100, 1, 5000),
                                                 ('freezetime', 0, 1, 12000), ('freezetime', 0, 1, 1000)):
                manager.process_gsi({'round': {'phase': phase}, 'player': {'team': 'T', 'activity': 'playing',
                    'state': {'health': 100, 'flashed': flashed, 'money': money}, 'match_stats': {'kills': kills}}})
        self.assertTrue({'kill', 'flashed', 'rich', 'poor'}.issubset({call.args[0] for call in triggered.call_args_list}))


if __name__ == '__main__': unittest.main()
