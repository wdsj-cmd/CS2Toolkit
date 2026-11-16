import time
import unittest
import wave
from pathlib import Path

from PySide6.QtCore import Signal

from app.logic.music_box_events import MusicCue
from app.logic.music_box_player import MusicBoxPlayer
import test_music_finish_before_switch as base

APP = base.APP


def pump(milliseconds):
    deadline = time.monotonic() + milliseconds / 1000
    while time.monotonic() < deadline:
        APP.processEvents(); time.sleep(.005)


class FadePlayer(base.Player):
    faded_out = Signal()
    def __init__(self): super().__init__(); self.fade_requests = []
    def fade_stop(self, milliseconds=1200): self.fade_requests.append(milliseconds); return True


class TimeoutMusicTests(unittest.TestCase):
    setUp = base.FinishBeforeSwitchTests.setUp
    assign = base.FinishBeforeSwitchTests.assign
    names = base.FinishBeforeSwitchTests.names

    def packet(self, pause=None, phase='freezetime', activity='playing', seconds='30'):
        data = {'provider': {'steamid': 'self'}, 'map': {'name': 'de_dust2', 'phase': 'live', 'round': 1},
            'round': {'phase': phase}, 'player': {'steamid': 'self', 'team': 'T',
                'activity': activity, 'state': {'health': 100}, 'match_stats': {'mvps': 0}}}
        if pause is not None:
            data['phase_countdowns'] = {'phase': pause, 'phase_ends_in': seconds}
        self.controller.process_gsi(data)

    def prepare(self, clip=True):
        self.player = FadePlayer()
        self.controller.player = self.player
        self.player.failed.connect(self.controller._failed)
        self.player.finished.connect(self.controller._finished)
        self.player.loop_finished.connect(self.controller._repeat_cue)
        self.player.faded_out.connect(self.controller._pause_fade_finished)
        if clip: self.assign('timeout', protected=False, duration=0, loop=True)
        self.packet()

    def test_all_pause_phases_start_once_and_esc_does_not_start_timeout_music(self):
        self.prepare()
        self.packet(activity='menu')
        self.assertEqual(self.names(), ['freeze_time'])
        for phase in ('timeout_t', 'timeout_t', 'timeout_ct', 'paused'):
            self.packet(phase)
        self.assertEqual(self.names(), ['freeze_time', 'timeout'])

    def test_sparse_packets_and_zero_seconds_do_not_falsely_end_pause(self):
        self.prepare(); self.packet('timeout_t')
        self.packet(); self.packet('timeout_t', seconds='0')
        self.assertTrue(self.controller._pause_active)
        self.assertFalse(self.player.fade_requests)
        self.assertEqual(self.names(), ['freeze_time', 'timeout'])

    def test_resume_fades_before_restoring_phase_music(self):
        self.prepare(); self.packet('timeout_t')
        self.packet('freezetime')
        self.assertEqual(self.player.fade_requests, [1200])
        self.assertEqual(self.names(), ['freeze_time', 'timeout'])
        self.player.faded_out.emit()
        self.assertEqual(self.names(), ['freeze_time', 'timeout', 'freeze_time'])

    def test_stage_changes_during_fade_resume_latest_stage_only(self):
        self.prepare(); self.packet('timeout_t'); self.packet('freezetime')
        self.packet('live', phase='live')
        self.assertEqual(self.names(), ['freeze_time', 'timeout'])
        self.player.faded_out.emit()
        self.assertEqual(self.names()[-1], 'round_start')

    def test_new_pause_during_old_fade_ignores_stale_fade_completion(self):
        self.prepare(); self.packet('timeout_t'); self.packet('freezetime')
        self.packet('timeout_ct')
        self.player.faded_out.emit()
        self.assertEqual(self.names(), ['freeze_time', 'timeout', 'timeout'])
        self.assertTrue(self.controller._pause_active)
        self.assertTrue(self.controller._pause_music_playing)

    def test_stop_and_disable_during_fade_do_not_restart_music(self):
        for disable in (False, True):
            with self.subTest(disable=disable):
                self.controller.set_enabled(True); self.prepare()
                self.packet('timeout_t'); self.packet('freezetime')
                if disable: self.controller.set_enabled(False)
                else: self.controller.stop()
                count = len(self.player.calls)
                self.player.faded_out.emit()
                self.assertEqual(len(self.player.calls), count)

    def test_preview_during_fade_is_not_interrupted_by_old_completion(self):
        self.prepare(); self.packet('timeout_t'); self.packet('freezetime')
        self.controller.preview('round_lost')
        self.player.faded_out.emit()
        self.assertTrue(self.controller.previewing)
        self.assertEqual(self.names()[-1], 'round_lost')

    def test_natural_end_while_paused_waits_without_restarting(self):
        self.prepare(); self.packet('timeout_t'); self.player.finished.emit()
        self.packet('timeout_t')
        self.assertEqual(self.names(), ['freeze_time', 'timeout'])
        self.packet('freezetime')
        self.assertFalse(self.player.fade_requests)
        self.assertEqual(self.names()[-1], 'freeze_time')

    def test_unconfigured_pause_preserves_existing_music(self):
        self.prepare(clip=False)
        stops = self.player.stops
        self.packet('timeout_t'); self.packet('freezetime')
        self.assertEqual(self.names(), ['freeze_time'])
        self.assertEqual(self.player.stops, stops)

    def test_pause_overrides_full_result_protection_and_timeout_loops(self):
        self.prepare()
        self.controller._play_cue(MusicCue('mvp', time.monotonic()))
        self.assertIsNotNone(self.controller._protected_cue)
        self.packet('timeout_t')
        self.assertEqual(self.names()[-1], 'timeout')
        self.assertIsNone(self.controller._protected_cue)
        self.player.loop_finished.emit()
        self.assertEqual(self.names()[-1], 'timeout')

    def test_team_menu_keeps_playing_if_pause_ends_while_menu_open(self):
        self.prepare(); self.packet('timeout_t'); self.controller._on_team_menu(True)
        count = len(self.player.calls)
        self.packet('freezetime')
        self.assertEqual(len(self.player.calls), count)
        self.assertEqual(self.names()[-1], 'team_select')
        self.controller._on_team_menu(False)
        self.assertEqual(self.names()[-1], 'freeze_time')

    def test_disconnect_cancels_pending_fade(self):
        self.prepare(); self.packet('timeout_t'); self.packet('freezetime')
        self.controller._last_gsi = time.monotonic() - 6
        self.controller._tick()
        count = len(self.player.calls); self.player.faded_out.emit()
        self.assertEqual(len(self.player.calls), count)
        self.assertFalse(self.controller._pause_active)

    def test_real_audio_fades_to_zero_before_next_music_starts(self):
        with wave.open(str(self.audio), 'wb') as stream:
            stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            stream.writeframes(b'\0\0' * 48000)
        self.assign('freeze_time', duration=0, loop=True)
        self.assign('timeout', duration=0, loop=True)
        player = MusicBoxPlayer()
        self.controller.player = player
        player.set_volume(80)
        player.failed.connect(self.controller._failed)
        player.finished.connect(self.controller._finished)
        player.loop_finished.connect(self.controller._repeat_cue)
        player.faded_out.connect(self.controller._pause_fade_finished)
        self.addCleanup(lambda: (player.cleanup(), pump(200)))
        self.packet(); self.packet('timeout_t')
        deadline = time.monotonic() + 3
        while not player._loaded and time.monotonic() < deadline: pump(10)
        self.assertTrue(player._loaded)
        pump(220)
        active = player.active
        initial = player.outputs[active].volume()
        self.packet('freezetime')
        self.assertTrue(player._fading_out)
        pump(250)
        middle = player.outputs[active].volume()
        self.assertGreater(middle, 0)
        self.assertLess(middle, initial)
        self.assertEqual(player.clip['name'], 'timeout')
        pump(1100)
        self.assertFalse(self.controller._pause_fade_pending)
        self.assertEqual(player.clip['name'], 'freeze_time')

    def test_new_audio_request_cancels_old_fade_timer(self):
        with wave.open(str(self.audio), 'wb') as stream:
            stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            stream.writeframes(b'\0\0' * 48000)
        player = MusicBoxPlayer()
        self.addCleanup(lambda: (player.cleanup(), pump(200)))
        faded = []
        player.faded_out.connect(lambda: faded.append(True))
        player.play(self.audio, {'start': 0, 'duration': 0, 'volume': 80, 'fade_ms': 0})
        deadline = time.monotonic() + 3
        while not player._loaded and time.monotonic() < deadline: pump(10)
        self.assertTrue(player.fade_stop(300))
        pump(80)
        player.play(self.audio, {'start': 0, 'duration': 0, 'volume': 80, 'fade_ms': 0})
        pump(450)
        self.assertGreaterEqual(player.active, 0)
        self.assertFalse(faded)


if __name__ == '__main__': unittest.main()
