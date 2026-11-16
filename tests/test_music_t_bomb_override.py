import time
import unittest
from pathlib import Path

from PySide6.QtWidgets import QWidget

from app.logic.music_box_events import BOMB_WARNING_EVENTS, MusicCue, SLOTS
from app.logic.music_box_library import event_options, validate_event_options
from app.logic.music_box_player import MusicBoxPlayer
from app.ui.pages.music_box_page import ClipDialog
import test_music_finish_before_switch as base

APP = base.APP

SELF = '76561198000000001'
OTHER = '76561198000000002'


class TerroristBombOverrideTests(unittest.TestCase):
    setUp = base.FinishBeforeSwitchTests.setUp
    names = base.FinishBeforeSwitchTests.names

    def assign(self, event, protected=True, duration=12, loop=False, override=False):
        self.controller.library.assign(self.kit['id'], event,
            [{'path': str(self.audio), 'name': event, 'start': .1, 'duration': duration, 'loop': loop}],
            {'finish_before_switch': protected, 't_round_end_override': override})
        if self.controller.kit:
            self.controller.reload_library()

    def packet(self, phase='live', team='T', bomb='', remaining=None, winner=None,
               mvps=0, round_number=1, player=SELF, activity='playing', map_name='de_dust2', round_bomb=''):
        state = {'provider': {'steamid': SELF},
            'map': {'name': map_name, 'phase': 'live', 'round': round_number},
            'round': {'phase': phase}, 'player': {'steamid': player, 'team': team,
                'activity': activity, 'state': {'health': 100}, 'match_stats': {'mvps': mvps}}}
        if bomb:
            state['bomb'] = {'state': bomb}
        if remaining is not None:
            state['bomb']['countdown'] = str(remaining)
        if winner:
            state['round']['win_team'] = winner
        if round_bomb:
            state['round']['bomb'] = round_bomb
        self.controller.process_gsi(state)
        return state

    def arm(self, event='bomb_ten_seconds', team='T', override=True, duration=12, loop=False):
        self.controller.stop(); self.controller.engine.reset(); self.player.calls.clear()
        self.now = 100.
        self.controller.engine.clock = lambda: self.now
        self.assign(event, protected=False, override=override, duration=duration, loop=loop)
        self.packet(team=team)
        if event != 'bomb_ten_seconds':
            state = {'provider': {'steamid': SELF}, 'map': {'name': 'de_dust2', 'phase': 'live', 'round': 1},
                'round': {'phase': 'live'}, 'player': {'steamid': SELF, 'team': team, 'activity': 'playing',
                    'state': {'health': 100}, 'match_stats': {'mvps': 0}},
                'bomb': {'state': 'planting', 'player': SELF if event.endswith('_self') else OTHER}}
            self.controller.process_gsi(state)
        self.packet(team=team, bomb='planted', remaining=9)
        self.assertEqual(self.names(), [event])

    def test_all_three_t_warning_tracks_replace_win_and_mvp_without_delaying_them_for_later(self):
        for event in BOMB_WARNING_EVENTS:
            with self.subTest(event=event):
                self.arm(event)
                self.packet('over', bomb='exploded', winner='T', mvps=1)
                self.assertEqual(self.names(), [event])
                self.assertFalse(self.controller._pending_result_cues)
                self.packet('freezetime', mvps=1, round_number=2)
                self.assertEqual(self.names(), [event])
                self.player.finished.emit()
                self.assertEqual(self.names(), [event, 'freeze_time'])

    def test_ct_and_disabled_switch_preserve_normal_mvp_playback(self):
        for team, enabled in (('CT', True), ('T', False)):
            with self.subTest(team=team, enabled=enabled):
                self.arm(team=team, override=enabled)
                self.packet('over', team=team, winner=team, mvps=1)
                self.assertEqual(self.names()[-1], 'mvp')

    def test_loss_interrupts_the_t_warning_even_with_complete_playback_enabled(self):
        self.arm()
        self.packet('over', bomb='defused', winner='CT')
        self.assertEqual(self.names(), ['bomb_ten_seconds', 'round_lost'])
        self.assertEqual(self.controller._protected_cue.event, 'round_lost')

    def test_loss_in_same_packet_as_mvp_plays_loss_and_late_mvp_does_not_restart_it(self):
        self.arm()
        self.assign('round_lost', protected=False)
        self.packet('over', bomb='defused', winner='CT', mvps=1)
        self.packet('over', bomb='defused', winner='CT', mvps=2)
        self.assertEqual(self.names(), ['bomb_ten_seconds', 'round_lost'])

    def test_late_winner_after_terminal_bomb_packet_does_not_cut_warning(self):
        self.arm()
        self.packet(bomb='exploded')
        self.assertEqual(self.names(), ['bomb_ten_seconds'])
        self.packet('over', bomb='exploded', winner='T')
        self.packet('over', bomb='exploded', winner='T', mvps=1)
        self.assertEqual(self.names(), ['bomb_ten_seconds'])

    def test_round_counter_increment_in_over_packet_does_not_drop_replacement(self):
        self.arm()
        self.packet('over', bomb='exploded', winner='T', round_number=2)
        self.packet('over', bomb='exploded', winner='T', mvps=1, round_number=2)
        self.assertEqual(self.names(), ['bomb_ten_seconds'])

    def test_finished_warning_still_replaces_late_result_for_its_round_only(self):
        self.arm()
        self.player.finished.emit()
        self.packet('over', bomb='exploded', winner='T', mvps=1)
        self.assertEqual(self.names(), ['bomb_ten_seconds'])
        self.packet('freezetime', mvps=1, round_number=2)
        self.packet('over', winner='T', mvps=2, round_number=2)
        self.assertEqual(self.names()[-2:], ['freeze_time', 'mvp'])

    def test_full_audio_and_loop_setting_play_exactly_one_pass(self):
        self.arm(duration=0, loop=True)
        clip = self.player.calls[-1][1]
        self.assertEqual((clip['duration'], clip['loop']), (0, False))

    def test_elimination_win_immediately_releases_all_three_warning_tracks(self):
        for event in BOMB_WARNING_EVENTS:
            with self.subTest(event=event):
                self.arm(event)
                self.packet('over', bomb='planted', winner='T')
                self.assertEqual(self.names(), [event, 'round_won'])
                self.assertFalse(self.controller._t_bomb_protected)
                self.assertIsNone(self.controller._t_bomb_override_round)

    def test_elimination_mvp_in_same_packet_immediately_replaces_warning(self):
        self.arm()
        self.packet('over', bomb='planted', winner='T', mvps=1)
        self.assertEqual(self.names(), ['bomb_ten_seconds', 'mvp'])

    def test_elimination_win_does_not_suppress_a_later_mvp(self):
        self.arm()
        self.assign('round_won', protected=False)
        self.packet('over', bomb='planted', winner='T')
        # An explosion arriving after an elimination result must not change
        # the already chosen outcome or suppress a subsequently awarded MVP.
        self.packet('over', bomb='exploded', winner='T', mvps=1)
        self.assertEqual(self.names(), ['bomb_ten_seconds', 'round_won', 'mvp'])

    def test_unknown_bomb_outcome_uses_normal_result_instead_of_assuming_explosion(self):
        self.arm()
        self.packet('over', winner='T', mvps=1)
        self.assertEqual(self.names(), ['bomb_ten_seconds', 'mvp'])

    def test_explosion_is_retained_for_a_later_mvp_packet_without_bomb_fields(self):
        self.arm()
        self.packet('over', bomb='exploded', winner='T')
        self.packet('over', winner='T', mvps=1)
        self.assertEqual(self.names(), ['bomb_ten_seconds'])
        self.assertTrue(self.controller._t_bomb_exploded)

    def test_round_summary_explosion_takes_precedence_over_a_lagging_detailed_node(self):
        self.arm()
        self.packet('over', bomb='planted', round_bomb='exploded', winner='T', mvps=1)
        self.assertEqual(self.names(), ['bomb_ten_seconds'])

    def test_explosion_from_previous_round_does_not_replace_next_round_elimination_result(self):
        self.arm()
        self.packet('over', bomb='exploded', winner='T')
        self.packet('freezetime', round_number=2)
        self.player.finished.emit()
        self.packet(bomb='planted', remaining=9, round_number=2)
        self.packet('over', bomb='planted', winner='T', mvps=1, round_number=2)
        self.assertEqual(self.names()[-1], 'mvp')

    def test_cached_local_t_team_is_used_while_observing_a_ct_player(self):
        self.arm()
        self.packet('over', team='CT', player=OTHER, activity='spectating', winner='T', bomb='exploded')
        self.assertEqual(self.controller.engine.team, 'T')
        self.assertEqual(self.names(), ['bomb_ten_seconds'])

    def test_observed_t_player_does_not_make_a_local_ct_warning_protected(self):
        self.arm(team='CT')
        self.packet(team='T', player=OTHER, activity='spectating', bomb='planted', remaining=8)
        self.assertEqual(self.controller.engine.team, 'CT')
        self.assertIsNone(self.controller._protected_cue)

    def test_switching_sides_or_maps_releases_old_t_warning(self):
        for change in ({'team': 'CT'}, {'map_name': 'de_mirage'}):
            with self.subTest(change=change):
                self.arm()
                self.packet('freezetime', round_number=2, **change)
                self.assertEqual(self.names()[-1], 'freeze_time')

    def test_generic_fallback_uses_the_generic_switch(self):
        self.arm()
        self.controller.stop()
        self.controller._play_cue(MusicCue('bomb_ten_seconds_other', self.now + 1))
        self.assertTrue(self.controller._t_bomb_protected)
        self.assertEqual(self.names()[-1], 'bomb_ten_seconds')

    def test_specific_track_uses_its_own_switch_instead_of_the_generic_setting(self):
        self.arm()
        self.arm('bomb_ten_seconds_other', override=False)
        self.packet('over', winner='T', mvps=1)
        self.assertEqual(self.names(), ['bomb_ten_seconds_other', 'mvp'])

    def test_failure_without_a_failure_clip_stops_warning_without_playing_mvp(self):
        self.arm()
        self.controller.library.assign(self.kit['id'], 'round_lost', [])
        self.controller.reload_library()
        stopped = self.player.stops
        self.packet('over', winner='CT', mvps=1)
        self.assertEqual(self.names(), ['bomb_ten_seconds'])
        self.assertGreater(self.player.stops, stopped)
        self.assertIsNone(self.controller._protected_cue)

    def test_stop_preview_and_decode_error_cancel_replacement(self):
        for action in ('stop', 'preview', 'error'):
            with self.subTest(action=action):
                self.arm()
                if action == 'stop': self.controller.stop()
                elif action == 'preview': self.controller.preview('round_won')
                else: self.player.failed.emit('decoder error')
                self.assertIsNone(self.controller._t_bomb_override_round)
                self.assertFalse(self.controller._t_bomb_protected)

    def test_old_kit_defaults_off_and_export_import_preserves_each_switch(self):
        self.assertFalse(validate_event_options(None, 1)['t_round_end_override'])
        with self.assertRaises(ValueError): validate_event_options({'t_round_end_override': 'true'}, 1)
        for event in BOMB_WARNING_EVENTS:
            self.assign(event, protected=False, override=event != 'bomb_ten_seconds_self')
        output = Path(self.temp.name)/'share.zip'
        self.controller.library.export(self.kit['id'], output)
        imported = self.controller.library.import_zip(output)
        for event in BOMB_WARNING_EVENTS:
            self.assertEqual(event_options(imported, event)['t_round_end_override'], event != 'bomb_ten_seconds_self')

    def test_only_bomb_warning_dialogs_show_the_t_switch(self):
        parent = QWidget(); self.addCleanup(parent.close)
        for slot in SLOTS:
            dialog = ClipDialog(self.controller, slot.key, parent)
            self.assertEqual(dialog.t_round_end_override is not None, slot.key in BOMB_WARNING_EVENTS)
            if dialog.t_round_end_override:
                self.assertFalse(dialog.t_round_end_override.isChecked())
                dialog.t_round_end_override.setChecked(True)
                self.assertTrue(dialog.playback_options()['t_round_end_override'])
            dialog.close(); dialog.deleteLater()

    def test_real_qt_completion_plays_pending_preparation_after_a_full_warning(self):
        player = MusicBoxPlayer()
        self.controller.player = player
        player.set_volume(0)
        player.failed.connect(self.controller._failed)
        player.finished.connect(self.controller._finished)
        def release_audio():
            player.cleanup()
            deadline = time.monotonic() + .2
            while time.monotonic() < deadline: APP.processEvents(); time.sleep(.005)
        self.addCleanup(release_audio)
        self.controller.stop(); self.controller.engine.reset()
        self.controller.engine.team = 'T'
        self.controller.engine.map_name = 'de_dust2'
        self.controller.engine.round_number = 1
        self.controller.engine.phase = 'live'
        self.assign('bomb_ten_seconds', protected=False, override=True, duration=.2)
        records = []
        self.controller.diagnostic.connect(lambda name, details: records.append(details['cue']) if name == 'music_cue' else None)
        self.controller._play_cue(MusicCue('bomb_ten_seconds', time.monotonic()))
        self.controller.engine.bomb_state = 'exploded'
        self.controller._play_cue(MusicCue('round_won', time.monotonic()))
        self.controller._play_cue(MusicCue('freeze_time', time.monotonic()))
        self.assertEqual(records, ['bomb_ten_seconds'])
        deadline = time.monotonic() + 3
        while len(records) < 2 and time.monotonic() < deadline: APP.processEvents(); time.sleep(.005)
        self.assertEqual(records, ['bomb_ten_seconds', 'freeze_time'])


if __name__ == '__main__': unittest.main()
