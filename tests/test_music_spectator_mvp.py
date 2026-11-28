import copy
import re
import unittest

from app.logic.gsi_manager import GSIManager
from app.logic.music_box_events import MusicEventEngine
import test_music_finish_before_switch as base


SELF = '76561198000000001'
OTHER = '76561198000000002'


def packet(phase='live', *, player=SELF, mvps=2, activity='playing', spectarget=None,
           health=100, winner=None, local_mvps=None, map_phase='live', round_number=4):
    state = {'provider': {'steamid': SELF},
             'map': {'name': 'de_dust2', 'phase': map_phase, 'round': round_number},
             'round': {'phase': phase},
             'player': {'steamid': player, 'team': 'CT', 'activity': activity,
                        'state': {'health': health}, 'match_stats': {'mvps': mvps}}}
    if spectarget is not None:
        state['player']['spectarget'] = spectarget
    if winner:
        state['round']['win_team'] = winner
    if local_mvps is not None:
        state['allplayers'] = {
            SELF: {'team': 'CT', 'match_stats': {'mvps': local_mvps}},
            OTHER: {'team': 'CT', 'match_stats': {'mvps': mvps}}}
    return state


class SpectatorMvpEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = MusicEventEngine(clock=lambda: 100.)
        self.engine.process(packet())
        self.engine.process(packet(health=0))

    def test_local_scoreboard_award_is_read_while_spectating_teammate(self):
        cue = self.engine.process(packet('over', player=OTHER, mvps=7, local_mvps=3, winner='CT'))
        self.assertEqual(cue.event, 'mvp')
        self.assertEqual(self.engine.round_result, 'round_won')

    def test_local_identity_stats_still_belong_to_self_with_spectator_target(self):
        cue = self.engine.process(packet('over', mvps=3, spectarget=OTHER, health=0, winner='CT'))
        self.assertEqual(cue.event, 'mvp')

    def test_local_identity_stats_still_belong_to_self_during_spectating_activity(self):
        cue = self.engine.process(packet('over', mvps=3, activity='spectating', health=0, winner='CT'))
        self.assertEqual(cue.event, 'mvp')

    def test_late_award_after_win_music_is_emitted_once(self):
        self.assertEqual(self.engine.process(packet('over', player=OTHER, local_mvps=2, winner='CT')).event,
                         'round_won')
        award = packet('over', player=OTHER, local_mvps=3, winner='CT')
        cue = self.engine.process(award)
        self.assertIsNotNone(cue)
        self.assertEqual(cue.event, 'mvp')
        self.assertIsNone(self.engine.process(copy.deepcopy(award)))

    def test_sparse_award_after_confirmed_round_end_is_not_consumed_without_music(self):
        self.engine.process(packet('over', player=OTHER, local_mvps=2, winner='CT'))
        award = packet('over', player=OTHER, local_mvps=3)
        del award['round']['phase']
        cue = self.engine.process(award)
        self.assertIsNotNone(cue)
        self.assertEqual(cue.event, 'mvp')
        self.assertIsNone(self.engine.process(packet('over', player=OTHER, local_mvps=3, winner='CT')))

    def test_first_preparation_packet_can_report_award_when_spectator_scoreboard_was_unavailable(self):
        self.engine.process(packet('over', player=OTHER, winner='CT'))
        cue = self.engine.process(packet('freezetime', mvps=3, round_number=5))
        self.assertIsNotNone(cue)
        self.assertEqual(cue.event, 'mvp')
        self.assertIsNone(self.engine.process(packet('freezetime', mvps=3, round_number=5)))

    def test_new_live_round_does_not_replay_old_mvp(self):
        self.engine.process(packet('over', player=OTHER, winner='CT'))
        self.assertEqual(self.engine.process(packet('freezetime', player=OTHER, round_number=5)).event,
                         'freeze_time')
        cue = self.engine.process(packet('live', mvps=3, round_number=5))
        self.assertEqual(cue.event, 'round_start')

    def test_skipped_rounds_do_not_replay_historical_award(self):
        self.engine.process(packet('over', player=OTHER, winner='CT'))
        cue = self.engine.process(packet('freezetime', mvps=3, round_number=8))
        self.assertEqual(cue.event, 'freeze_time')

    def test_observed_teammate_award_never_counts_as_local_mvp(self):
        cue = self.engine.process(packet('over', player=OTHER, mvps=20, local_mvps=2, winner='CT'))
        self.assertEqual(cue.event, 'round_won')
        self.assertEqual(self.engine.mvps, 2)

    def test_missing_local_scoreboard_entry_does_not_use_observed_stats(self):
        state = packet('over', player=OTHER, mvps=20, local_mvps=3, winner='CT')
        del state['allplayers'][SELF]
        self.assertEqual(self.engine.process(state).event, 'round_won')
        self.assertEqual(self.engine.mvps, 2)

    def test_cached_provider_identity_is_used_when_sparse_packet_omits_provider(self):
        state = packet('over', player=OTHER, mvps=20, local_mvps=3, winner='CT')
        del state['provider']
        self.assertEqual(self.engine.process(state).event, 'mvp')
        self.assertEqual(self.engine.mvps, 3)

    def test_final_match_scoreboard_allows_spectating_local_award(self):
        cue = self.engine.process(packet('freezetime', player=OTHER, local_mvps=3,
                                         map_phase='gameover', winner='CT'))
        self.assertEqual(cue.event, 'mvp')

    def test_first_local_scoreboard_counter_is_baseline_without_retroactive_award(self):
        engine = MusicEventEngine(clock=lambda: 100.)
        engine.process(packet(player=OTHER, local_mvps=3))
        cue = engine.process(packet('over', player=OTHER, local_mvps=3, winner='CT'))
        self.assertTrue(cue is None or cue.event == 'round_won')
        self.assertEqual(engine.mvps, 3)
        # A genuine subsequent increase must still be detected.
        cue = engine.process(packet('over', player=OTHER, local_mvps=4, winner='CT'))
        self.assertIsNotNone(cue)
        self.assertEqual(cue.event, 'mvp')

    def test_observed_mvp_without_provider_or_local_stats_does_not_increase_local_counter(self):
        state = packet('over', player=OTHER, mvps=20, winner='CT')
        del state['provider']
        self.assertEqual(self.engine.process(state).event, 'round_won')
        self.assertEqual(self.engine.mvps, 2)

    def test_switching_provider_establishes_a_new_mvp_baseline(self):
        state = packet('over', player=OTHER, mvps=20, winner='CT')
        state['provider']['steamid'] = OTHER
        self.assertEqual(self.engine.process(state).event, 'round_won')
        self.assertEqual(self.engine.mvps, 20)


class SpectatorMvpPlaybackTests(unittest.TestCase):
    setUp = base.FinishBeforeSwitchTests.setUp
    assign = base.FinishBeforeSwitchTests.assign
    names = base.FinishBeforeSwitchTests.names

    def test_spectator_award_switches_from_unprotected_win_music_to_mvp(self):
        self.assign('round_won', protected=False)
        self.controller.process_gsi(packet())
        self.controller.process_gsi(packet(health=0))
        self.controller.process_gsi(packet('over', player=OTHER, local_mvps=2, winner='CT'))
        self.assertEqual(self.names(), ['round_won'])
        award = packet('over', player=OTHER, local_mvps=3, winner='CT')
        self.controller.process_gsi(award)
        self.controller.process_gsi(copy.deepcopy(award))
        self.assertEqual(self.names(), ['round_won', 'mvp'])

    def test_spectator_award_waits_for_protected_win_track_then_plays(self):
        self.controller.process_gsi(packet())
        self.controller.process_gsi(packet('over', player=OTHER, local_mvps=2, winner='CT'))
        self.controller.process_gsi(packet('over', player=OTHER, local_mvps=3, winner='CT'))
        self.assertEqual(self.names(), ['round_won'])
        self.player.finished.emit()
        self.assertEqual(self.names(), ['round_won', 'mvp'])

    def test_award_on_respawn_uses_mvp_then_resumes_preparation_music(self):
        self.assign('round_won', protected=False)
        self.controller.process_gsi(packet())
        self.controller.process_gsi(packet('over', player=OTHER, winner='CT'))
        self.controller.process_gsi(packet('freezetime', mvps=3, round_number=5))
        self.assertEqual(self.names(), ['round_won', 'mvp'])
        self.player.finished.emit()
        self.assertEqual(self.names(), ['round_won', 'mvp', 'freeze_time'])

    def test_award_on_respawn_keeps_win_fallback_when_no_mvp_clip_is_configured(self):
        self.assign('round_won', protected=False)
        self.controller.library.assign(self.kit['id'], 'mvp', [], {'finish_before_switch': False})
        self.controller.reload_library()
        self.controller.process_gsi(packet())
        self.controller.process_gsi(packet('over', player=OTHER, winner='CT'))
        self.controller.process_gsi(packet('freezetime', mvps=3, round_number=5))
        self.assertEqual(self.names(), ['round_won', 'round_won'])
        self.player.finished.emit()
        self.assertEqual(self.names(), ['round_won', 'round_won', 'freeze_time'])

    def test_unprotected_respawn_mvp_naturally_returns_to_preparation(self):
        self.assign('round_won', protected=False)
        self.assign('mvp', protected=False)
        self.controller.process_gsi(packet())
        self.controller.process_gsi(packet('over', player=OTHER, winner='CT'))
        self.controller.process_gsi(packet('freezetime', mvps=3, round_number=5))
        self.assertEqual(self.names(), ['round_won', 'mvp'])
        self.player.finished.emit()
        self.assertEqual(self.names(), ['round_won', 'mvp', 'freeze_time'])

    def test_new_live_round_prevents_stale_preparation_after_unprotected_mvp(self):
        self.assign('round_won', protected=False)
        self.assign('mvp', protected=False)
        self.controller.process_gsi(packet())
        self.controller.process_gsi(packet('over', player=OTHER, winner='CT'))
        self.controller.process_gsi(packet('freezetime', mvps=3, round_number=5))
        self.controller.process_gsi(packet('live', mvps=3, round_number=5))
        self.player.finished.emit()
        self.assertEqual(self.names(), ['round_won', 'mvp', 'round_start'])

    def test_explosion_override_filters_late_mvp_without_losing_preparation_music(self):
        self.controller.library.assign(self.kit['id'], 'bomb_ten_seconds',
            [{'path': str(self.audio), 'name': 'warning', 'start': 0, 'duration': 12, 'loop': False}],
            {'finish_before_switch': True, 't_round_end_override': True})
        self.controller.reload_library()
        def terrorist(state):
            state['player']['team'] = 'T'
            return state
        self.controller.process_gsi(terrorist(packet()))
        bomb = terrorist(packet())
        bomb['bomb'] = {'state': 'planted', 'countdown': '9'}
        self.controller.process_gsi(bomb)
        result = terrorist(packet('over', player=OTHER, winner='T'))
        result['round']['bomb'] = 'exploded'
        self.controller.process_gsi(result)
        self.controller.process_gsi(terrorist(packet('freezetime', mvps=3, round_number=5)))
        self.player.finished.emit()
        self.assertEqual(self.names(), ['warning', 'freeze_time'])


class SpectatorScoreboardSubscriptionTests(unittest.TestCase):
    def test_gsi_requests_scoreboard_identity_and_stats_without_positions_or_weapons(self):
        config = GSIManager().get_gsi_cfg_content()
        data = config.split('"data"', 1)[1]
        subscriptions = dict(re.findall(r'"([a-z_]+)"\s+"([01])"', data))
        self.assertEqual({key: subscriptions[key] for key in ('allplayers_id', 'allplayers_match_stats',
                          'allplayers_position', 'allplayers_state', 'allplayers_weapons')},
                         {'allplayers_id': '1', 'allplayers_match_stats': '1', 'allplayers_position': '0',
                          'allplayers_state': '0', 'allplayers_weapons': '0'})


if __name__ == '__main__':
    unittest.main()
