import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import wave

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QWidget

from app.logic.music_box_controller import MusicBoxController
from app.logic.music_box_events import MusicCue, FINISH_BEFORE_SWITCH_EVENTS, SLOTS
from app.logic.music_box_library import event_options, validate_event_options
from app.logic.music_box_player import MusicBoxPlayer
from app.ui.pages.music_box_page import ClipDialog

APP = QApplication.instance() or QApplication([])


class Config:
    def __init__(self, root): self.work_dir, self.values = root, {}
    def get(self, key, default=None): return self.values.get(key, default)
    def set(self, key, value): self.values[key] = copy.deepcopy(value)


class Monitor(QObject):
    detected = Signal(float)
    status_changed = Signal(str)
    team_menu_changed = Signal(bool)
    diagnostic = Signal(str, dict)
    sampled = Signal(object, int, float)
    resync_requested = Signal(object)
    def __init__(self, *args): super().__init__()
    def start(self): pass
    def stop(self): pass
    def set_context(self, *args, **kwargs): pass


class Player(QObject):
    failed = Signal(str)
    finished = Signal()
    loop_finished = Signal()
    def __init__(self): super().__init__(); self.calls = []; self.stops = 0
    def play(self, path, clip): self.calls.append((path, dict(clip))); return True
    def set_volume(self, value): pass
    def stop(self): self.stops += 1
    def cleanup(self): self.stop()


class FinishBeforeSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Config(self.temp.name)
        self.player = Player()
        with patch('app.logic.music_box_controller.BombVisualMonitor', Monitor), \
                patch('app.logic.music_box_controller.RoundClockMonitor', Monitor):
            self.controller = MusicBoxController(self.config, player=self.player)
        self.addCleanup(self.controller.cleanup)
        self.audio = Path(self.temp.name)/'clip.wav'
        with wave.open(str(self.audio), 'wb') as stream:
            stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            stream.writeframes(b'\0\0' * 4800)
        self.kit = self.controller.library.create('test')
        for event in ('round_won', 'round_lost', 'mvp', 'freeze_time', 'round_start', 'main_menu', 'team_select'):
            self.assign(event, protected=event in FINISH_BEFORE_SWITCH_EVENTS)
        self.controller.select_kit(self.kit['id'])
        self.controller.set_enabled(True)

    def assign(self, event, protected=True, duration=12, loop=False):
        self.controller.library.assign(self.kit['id'], event,
            [{'path': str(self.audio), 'name': event, 'start': .1, 'duration': duration, 'loop': loop}],
            {'finish_before_switch': protected})
        if self.controller.kit:
            self.controller.reload_library()

    def play(self, event, started=None):
        cue = MusicCue(event, time.monotonic() if started is None else started)
        self.controller.engine.current_cue = cue
        self.controller._play_cue(cue)
        return cue

    def names(self): return [clip['name'] for _, clip in self.player.calls]

    def test_each_result_waits_for_duration_completion_before_preparation(self):
        for event in FINISH_BEFORE_SWITCH_EVENTS:
            with self.subTest(event=event):
                self.controller.stop(); self.player.calls.clear()
                self.play(event); self.play('freeze_time')
                self.assertEqual(self.names(), [event])
                self.assertEqual(self.player.calls[-1][1]['duration'], 12)
                self.player.finished.emit()
                self.assertEqual(self.names(), [event, 'freeze_time'])

    def test_disabled_option_preserves_immediate_event_switch(self):
        self.assign('mvp', protected=False)
        self.play('mvp'); self.play('freeze_time')
        self.assertEqual(self.names(), ['mvp', 'freeze_time'])

    def test_win_then_mvp_then_latest_game_event_are_not_lost(self):
        self.play('round_won'); self.play('mvp'); self.play('freeze_time'); self.play('round_start')
        self.player.finished.emit()
        self.assertEqual(self.names(), ['round_won', 'mvp'])
        self.player.finished.emit()
        self.assertEqual(self.names(), ['round_won', 'mvp', 'round_start'])

    def test_deferred_mvp_keeps_its_fallback_after_round_result_resets(self):
        self.controller.library.assign(self.kit['id'], 'mvp', [], {'finish_before_switch': True})
        self.controller.reload_library()
        self.controller.engine.round_result = 'round_won'
        self.play('round_won'); self.play('mvp')
        self.controller.engine.round_result = ''
        self.play('freeze_time'); self.player.finished.emit()
        self.assertEqual(self.names(), ['round_won', 'round_won'])
        self.assertEqual(self.controller._protected_cue.event, 'mvp')
        self.player.finished.emit()
        self.assertEqual(self.names()[-1], 'freeze_time')

    def test_full_file_and_loop_use_one_complete_pass(self):
        self.assign('mvp', duration=0, loop=True)
        self.play('mvp'); self.play('freeze_time')
        clip = self.player.calls[-1][1]
        self.assertEqual((clip['start'], clip['duration'], clip['loop']), (.1, 0, False))
        self.player.finished.emit()
        self.assertEqual(self.names(), ['mvp', 'freeze_time'])

    def test_repeated_current_cue_does_not_restart_or_redraw(self):
        cue = self.play('mvp')
        self.controller._play_cue(cue, resume=True)
        self.assertEqual(self.names(), ['mvp'])

    def test_waiting_does_not_skip_the_beginning_of_deferred_music(self):
        self.play('mvp'); self.play('round_won', time.monotonic() - 60)
        self.player.finished.emit()
        self.assertEqual(self.player.calls[-1][1]['start'], .1)
        self.assertEqual(self.player.calls[-1][1]['duration'], 12)

    def test_manual_stop_cancels_pending_music(self):
        self.play('mvp'); self.play('freeze_time'); self.controller.stop()
        self.player.finished.emit()
        self.assertEqual(self.names(), ['mvp'])

    def test_disabling_or_changing_kit_cancels_pending_music(self):
        self.play('mvp'); self.play('freeze_time'); self.controller.set_enabled(False)
        self.player.finished.emit(); self.assertEqual(self.names(), ['mvp'])
        self.controller.set_enabled(True); self.controller.stop(); self.player.calls.clear()
        self.play('mvp'); self.play('freeze_time')
        other = self.controller.library.create('other')
        self.controller.select_kit(other['id']); self.player.finished.emit()
        self.assertEqual(self.names(), ['mvp'])

    def test_preview_and_team_menu_remain_immediate_manual_overrides(self):
        self.play('mvp'); self.play('freeze_time'); self.controller.preview('round_lost')
        self.assertEqual(self.names()[-1], 'round_lost')
        self.controller.end_preview(); self.play('mvp'); self.controller._on_team_menu(True)
        self.assertEqual(self.names()[-1], 'team_select')

    def test_playback_error_releases_waiting_event(self):
        self.play('mvp'); self.play('freeze_time')
        self.player.failed.emit('decoder error')
        self.assertEqual(self.names(), ['mvp', 'freeze_time'])

    def test_connection_loss_cancels_waiting_event(self):
        self.play('mvp'); self.play('freeze_time')
        self.controller._last_gsi = time.monotonic() - 6
        self.controller._tick(); self.player.finished.emit()
        self.assertEqual(self.names(), ['mvp'])

    def test_options_survive_export_import_and_old_kits_default_off(self):
        output = Path(self.temp.name)/'kit.zip'
        self.controller.library.export(self.kit['id'], output)
        imported = self.controller.library.import_zip(output)
        for event in FINISH_BEFORE_SWITCH_EVENTS:
            self.assertTrue(event_options(imported, event)['finish_before_switch'])
        self.assertFalse(validate_event_options(None, 1)['finish_before_switch'])
        with self.assertRaises(ValueError): validate_event_options({'finish_before_switch': 'true'}, 1)

    def test_only_three_result_dialogs_have_independent_switches(self):
        parent = QWidget()
        self.addCleanup(parent.close)
        for slot in SLOTS:
            dialog = ClipDialog(self.controller, slot.key, parent)
            self.assertEqual(dialog.finish_before_switch is not None, slot.key in FINISH_BEFORE_SWITCH_EVENTS)
            if dialog.finish_before_switch is not None:
                self.assertTrue(dialog.finish_before_switch.isChecked())
                dialog.finish_before_switch.setChecked(False)
                self.assertFalse(dialog.playback_options()['finish_before_switch'])
            dialog.close(); dialog.deleteLater()

    def test_real_player_duration_and_end_of_file_release_deferred_event(self):
        self.controller.player = MusicBoxPlayer()
        def release_audio():
            self.controller.player.cleanup()
            deadline = time.monotonic() + .2
            while time.monotonic() < deadline:
                APP.processEvents(); time.sleep(.005)
        self.addCleanup(release_audio)
        self.controller.player.set_volume(0)
        self.controller.player.failed.connect(self.controller._failed)
        self.controller.player.finished.connect(self.controller._finished)
        for duration in (.2, 0):
            with self.subTest(duration=duration):
                self.controller.stop(); self.assign('mvp', duration=duration, loop=True)
                records = []
                def capture(name, details):
                    if name == 'music_cue': records.append(details['cue'])
                self.controller.diagnostic.connect(capture)
                self.play('mvp'); self.play('freeze_time')
                self.assertEqual(records, ['mvp'])
                deadline = time.monotonic() + 3
                while len(records) < 2 and time.monotonic() < deadline:
                    APP.processEvents(); time.sleep(.005)
                self.assertEqual(records[:2], ['mvp', 'freeze_time'])
                self.controller.diagnostic.disconnect(capture)


if __name__ == '__main__': unittest.main()
