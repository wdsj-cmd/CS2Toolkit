"""Bridge the shared GSI stream, kit storage and the dedicated Qt music player."""
import random
import time
import math

from PySide6.QtCore import QObject, QTimer, Signal

from .music_box_events import MusicEventEngine, MusicCue, SLOT_MAP, FINISH_BEFORE_SWITCH_EVENTS, BOMB_WARNING_EVENTS, object_node, number
from .music_box_library import MusicKitLibrary, event_options, validate_clip
from .music_box_player import MusicBoxPlayer
from .music_bomb_visual import BombVisualMonitor
from .music_round_clock import RoundClockMonitor

DEFAULT_SETTINGS = {'enabled': False, 'volume': 50, 'selected_kit': '',
                    'estimate_timers': True, 'bomb_seconds': 40, 'c4_warning_lead_ms': 500,
                    'gsi_plant_delay_ms': 0}


class MusicBoxController(QObject):
    changed = Signal()
    status_changed = Signal(str)
    error = Signal(str)
    diagnostic = Signal(str, dict)
    visual_status_changed = Signal(str)
    round_time_changed = Signal(object)

    def __init__(self, config_manager, parent=None, player=None):
        super().__init__(parent)
        self.config = config_manager
        self.library = MusicKitLibrary(config_manager.work_dir)
        self.settings = dict(DEFAULT_SETTINGS)
        saved = config_manager.get('music_box', {})
        if isinstance(saved, dict):
            self.settings.update({key: saved[key] for key in DEFAULT_SETTINGS if key in saved})
        for key, low, high in (('volume', 0, 100), ('bomb_seconds', 11, 180),
                               ('c4_warning_lead_ms', 0, 2000), ('gsi_plant_delay_ms', 0, 3000)):
            try:
                self.settings[key] = max(low, min(high, int(self.settings[key])))
            except (TypeError, ValueError, OverflowError):
                self.settings[key] = DEFAULT_SETTINGS[key]
        self.engine = MusicEventEngine(estimate_timers=self.settings['estimate_timers'],
                                       bomb_seconds=self.settings['bomb_seconds'],
                                       warning_lead_ms=self.settings['c4_warning_lead_ms'],
                                       gsi_plant_delay_ms=self.settings['gsi_plant_delay_ms'])
        self.player = player or MusicBoxPlayer(self)
        self.player.set_volume(self.settings['volume'])
        self.player.failed.connect(self._failed)
        self.player.finished.connect(self._finished)
        if hasattr(self.player, 'loop_finished'):
            self.player.loop_finished.connect(self._repeat_cue)
        if hasattr(self.player, 'started'):
            self.player.started.connect(self._audio_loaded)
        if hasattr(self.player, 'faded_out'):
            self.player.faded_out.connect(self._pause_fade_finished)
        self._audio_request = None
        self._cue_choice = None
        self._protected_cue = None
        self._t_bomb_protected = False
        self._t_bomb_override_round = None
        self._t_bomb_loss_started = False
        self._t_bomb_exploded = False
        self._pending_result_cues = []
        self._pending_cue = None
        self._result_return_cue = None
        self._last_random_clip = {}
        self._bomb_signature = None
        self._bomb_input_signature = None
        self._team_state_signature = None
        self._mvp_signature = None
        self._pause_watch_signature = None
        self.kit = None
        self.previewing = False
        self.status = '未启用，准备好音乐切片后即可配置'
        self._last_gsi = None
        self.round_seconds_remaining = None
        self.round_time_source = ''
        self._clock_map_live = self._clock_blocked = self._clock_menu = False
        self._closed = False
        self._team_menu_open = False
        self._team_cue = self._team_return_choice = None
        self._pause_active = self._pause_music_playing = self._pause_was_played = False
        self._pause_fade_pending = False
        self._pause_background_hold = False
        self._pause_was_visual = False
        self._pause_round_key = None
        self._game_window_exists = self._game_foreground = None
        self._pause_cue = self._pause_return_choice = None
        self.visual_monitor = BombVisualMonitor(config_manager.work_dir, self)
        self.visual_monitor.detected.connect(self._on_visual_bomb)
        self.visual_monitor.status_changed.connect(self._visual_status)
        self.visual_monitor.team_menu_changed.connect(self._on_team_menu)
        self.visual_monitor.diagnostic.connect(self.diagnostic)
        self.clock_monitor = RoundClockMonitor(self)
        self.clock_monitor.sampled.connect(self._on_round_clock)
        self.clock_monitor.resync_requested.connect(self._on_clock_resync)
        if hasattr(self.clock_monitor, 'pause_sampled'):
            self.clock_monitor.pause_sampled.connect(self._on_pause_visual)
        if hasattr(self.clock_monitor, 'pause_diagnostic'):
            self.clock_monitor.pause_diagnostic.connect(lambda details: self.diagnostic.emit('music_timeout_detection', details))
        if hasattr(self.clock_monitor, 'pause_status_changed'):
            self.clock_monitor.pause_status_changed.connect(lambda details: self.diagnostic.emit('music_timeout_sample', details))
        if hasattr(self.clock_monitor, 'game_visibility_changed'):
            self.clock_monitor.game_visibility_changed.connect(self._on_game_visibility)
        self.clock_monitor.status_changed.connect(lambda text: self.diagnostic.emit('music_round_clock_status', {'status': text}))
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.timeout.connect(self._save)
        self.tick_timer = QTimer(self)
        self.tick_timer.timeout.connect(self._tick)
        self.tick_timer.start(200)
        self.reload_library()
        if self.enabled:
            self._status('已启用，等待游戏数据')
        self._sync_visual_monitor()

    @property
    def enabled(self):
        return bool(self.settings['enabled'])

    def _status(self, text):
        self.status = text
        self.status_changed.emit(text)

    def _save(self):
        self.config.set('music_box', dict(self.settings))

    def _queue_save(self):
        self.save_timer.start(250)

    def reload_library(self):
        self._cue_choice = None
        kits = self.library.list_kits()
        selected = next((kit for kit in kits if kit['id'] == self.settings['selected_kit']), None)
        if selected is None and kits:
            selected = kits[0]
        self.kit = selected
        selected_id = selected['id'] if selected else ''
        if selected_id != self.settings['selected_kit']:
            self.settings['selected_kit'] = selected_id
            self._queue_save()
        self.changed.emit()

    def select_kit(self, kit_id):
        self.stop()
        self._cue_choice = None
        self.kit = self.library.load(kit_id) if kit_id else None
        self.settings['selected_kit'] = kit_id
        self._queue_save()
        if self.enabled:
            self._resume_music()
        self.changed.emit()

    def set_enabled(self, enabled):
        self.settings['enabled'] = bool(enabled)
        self._queue_save()
        if not enabled:
            self._team_menu_open = False
            self._team_cue = self._team_return_choice = None
            self.stop()
            self._status('创意音乐盒已关闭')
        else:
            self._status('已启用，等待游戏事件' if self.kit else '请先创建或导入一个音乐盒')
            self._resume_music()
        self._sync_visual_monitor()
        self.changed.emit()

    def _sync_visual_monitor(self):
        if self.enabled and not self._closed:
            self.visual_monitor.start()
            self.clock_monitor.start()
        else:
            self.visual_monitor.stop()
            self.clock_monitor.stop()
        self._update_clock_context()

    def _update_clock_context(self):
        key = self.engine.map_name, self.engine.round_number
        received = time.monotonic() if self._pause_active else self._last_gsi if self._last_gsi is not None else time.monotonic() - 4
        eligible = (self.enabled and not self._closed and self._clock_map_live and not self._clock_blocked
                  and not self._clock_menu and not self._team_menu_open and self.engine.phase == 'live'
                  and not self.engine.round_paused and not self._pause_active and self.engine.bomb_state not in ('planted', 'defusing'))
        self.clock_monitor.set_context(key, eligible and self.engine.round_deadline is None, received,
            watch=self._pause_active or eligible and self.engine.round_clock_source == 'hud')
        if hasattr(self.clock_monitor, 'set_pause_context'):
            clips = self.kit['tracks'].get('timeout', []) if self.kit else []
            checks = {'enabled': self.enabled, 'open': not self._closed, 'map_live': self._clock_map_live,
                'not_flashed': not self._clock_blocked, 'not_menu': not self._clock_menu or self._pause_active,
                'not_team_menu': not self._team_menu_open, 'playable_phase': self.engine.phase in ('freezetime', 'live'),
                'needs_visual': not self.engine.pause_gsi_available, 'has_timeout_clip': bool(clips)}
            pause_watch = all(checks.values())
            self.clock_monitor.set_pause_context(key, pause_watch, received, self.engine.phase == 'live', self._pause_active)
            signature = tuple(checks.values()), len(clips), self.engine.phase, self.previewing
            if signature != self._pause_watch_signature:
                self._pause_watch_signature = signature
                self.diagnostic.emit('music_timeout_watch', {'ready': pause_watch,
                    'blocked_by': [name for name, value in checks.items() if not value],
                    'round_phase': self.engine.phase, 'timeout_clip_count': len(clips), 'previewing': self.previewing})

    def _on_pause_visual(self, key, paused, observed_at):
        if (self._closed or not self.enabled or key != (self.engine.map_name, self.engine.round_number)
                or not self._clock_map_live or self._clock_blocked
                or (self._clock_menu and not self._pause_active) or self._team_menu_open):
            return
        changed = self.engine.confirm_pause_visual(paused, observed_at)
        if changed or (not paused and self._pause_active and self._pause_background_hold
                       and isinstance(observed_at, (float, int)) and 0 <= time.monotonic()-observed_at <= .75):
            self.diagnostic.emit('music_timeout_visual', {'active': paused})
            self._update_pause_music(visual_end=not paused)
            self._update_clock_context()
            self._update_round_time()
            self._update_tick_rate()

    def _on_game_visibility(self, exists, foreground):
        self._game_window_exists, self._game_foreground = exists, foreground
        if not exists and self._pause_active:
            self._clear_pause_state()
            self.player.stop()
        elif foreground and self._pause_background_hold:
            self._update_pause_music()
        self._update_clock_context()

    def _on_clock_resync(self, key):
        if (not self._closed and self.enabled and key == (self.engine.map_name, self.engine.round_number)
                and self.engine.round_clock_source == 'hud'):
            self.engine.round_deadline = None
            self.engine.round_clock_source = ''
            self.diagnostic.emit('music_round_clock_resync', {'reason': 'foreground_return'})
            self._update_clock_context()
            self._update_round_time()

    def _on_round_clock(self, key, seconds, observed_at):
        if (key != (self.engine.map_name, self.engine.round_number) or not self.enabled or self._closed
                or self._team_menu_open or self._clock_menu or self._clock_blocked):
            return
        before = self.engine.round_deadline
        cue = self.engine.confirm_round_clock(seconds, observed_at)
        if before is None and self.engine.round_deadline is not None:
            self.diagnostic.emit('music_round_clock_synced', {'seconds': seconds, 'source': 'hud',
                'capture_to_ui_ms': round((time.monotonic() - observed_at) * 1000, 1)})
        self._update_clock_context()  # The accepted anchor stops further OCR for this round.
        self._update_round_time()
        self._update_tick_rate()
        if cue and not self.previewing and not self._team_menu_open:
            self._play_cue(cue)

    def _visual_status(self, text):
        self.visual_status_changed.emit(text)
        self.diagnostic.emit('music_bomb_visual_status', {'status': text})

    def _resume_music(self):
        cue = self._display_cue()
        if cue:
            self._play_cue(cue, resume=True)
        else:
            self.player.stop()

    def _display_cue(self):
        if self._team_menu_open:
            return self._team_cue
        if self._pause_active and self.kit and self.kit['tracks'].get('timeout'):
            return self._pause_cue
        return self.engine.current_cue

    def _pause_owns_music(self):
        return bool(self._pause_fade_pending or (self._pause_active and self.kit
                    and self.kit['tracks'].get('timeout')))

    def _update_pause_music(self, visual_end=False):
        paused = bool(self.engine.round_paused and self.engine.map_name) and not visual_end
        if self._pause_active and not paused:
            background = self._game_window_exists is True and self._game_foreground is False
            same_round = self._pause_round_key == (self.engine.map_name, self.engine.round_number)
            waiting_for_timer = ((self._pause_background_hold or self._pause_was_visual)
                                 and same_round and self.engine.phase in ('freezetime', 'live')
                                 and self.engine.map_name and not self.engine.pause_gsi_available)
            if background or (waiting_for_timer and not visual_end):
                self._pause_background_hold = True
                return True
        if paused == self._pause_active:
            return False
        self._pause_active = paused
        self._pause_background_hold = False
        if paused:
            self._pause_was_visual = self.engine.pause_source == 'hud'
            self._pause_round_key = self.engine.map_name, self.engine.round_number
            self._pause_cue = MusicCue('timeout', time.monotonic())
            if not self._pause_fade_pending:
                self._pause_return_choice = self._cue_choice
            self._pause_fade_pending = False
            self._pause_was_played = False
            self.diagnostic.emit('music_timeout', {'active': True, 'source': self.engine.pause_source,
                                                  'field': self.engine.pause_field})
            if self.enabled and not self.previewing and not self._team_menu_open and self._pause_owns_music():
                self._play_cue(self._pause_cue)
                return True
            return False
        self._pause_cue = None
        self._pause_was_visual = False
        self._pause_round_key = None
        fade_ms = 0 if visual_end else 1200
        self.diagnostic.emit('music_timeout', {'active': False, 'fade_ms': fade_ms})
        if not self._pause_was_played:
            self._pause_return_choice = None
            return False
        self._pause_was_played = False
        self._pause_fade_pending = True
        playing, self._pause_music_playing = self._pause_music_playing, False
        if playing and self.enabled and not self.previewing and not self._team_menu_open:
            fade = getattr(self.player, 'fade_stop', None)
            if fade_ms and fade is not None and fade(fade_ms):
                self._status('暂停结束，音乐正在渐弱…')
                return True
            if not fade_ms or fade is None:
                self.player.stop()
        self._pause_fade_finished()
        return True

    def _pause_fade_finished(self):
        if not self._pause_fade_pending or self._closed:
            return
        self._pause_fade_pending = False
        if self._team_menu_open:
            self._team_return_choice = self._pause_return_choice
        else:
            self._cue_choice = self._pause_return_choice
        self._pause_return_choice = None
        if self.enabled and not self.previewing and not self._team_menu_open:
            self._resume_music()

    def _cancel_pause_fade(self):
        if self._pause_fade_pending:
            self._pause_fade_pending = False
            self._cue_choice = self._pause_return_choice
            self._pause_return_choice = None
        self._pause_music_playing = False

    def _clear_pause_state(self):
        self._pause_active = self._pause_music_playing = self._pause_was_played = False
        self._pause_fade_pending = False
        self._pause_background_hold = False
        self._pause_was_visual = False
        self._pause_round_key = None
        self._pause_cue = self._pause_return_choice = None

    def _on_team_menu(self, visible):
        if self._closed or visible == self._team_menu_open:
            return
        self._cancel_pause_fade()
        self._clear_playback_protection()
        self._team_menu_open = visible
        already_playing = False
        if visible:
            was_previewing = self.previewing
            if self.enabled:
                self.previewing = False
            self._team_return_choice = self._cue_choice
            current = self.engine.current_cue
            already_playing = bool(not was_previewing and current and current.event == 'team_select' and self._cue_choice)
            self._team_cue = current if current and current.event == 'team_select' else MusicCue('team_select', time.monotonic())
        else:
            self._cue_choice = self._team_return_choice
            self._team_cue = self._team_return_choice = None
        self.diagnostic.emit('music_team_menu', {'visible': visible})
        if visible and self.engine.round_clock_source == 'hud':
            self.engine.round_deadline = None
            self.engine.round_clock_source = ''
        self._update_clock_context()
        if self.enabled and not self.previewing and not already_playing:
            if not visible and self.engine.current_cue and self.engine.current_cue.event == 'team_select':
                self.player.stop()
                self._status('队伍菜单已关闭，等待游戏事件')
            else:
                self._resume_music()

    def reset_visual_template(self):
        try:
            if self.visual_monitor.reset_template():
                self._sync_visual_monitor()
        except OSError as exc:
            self.error.emit(str(exc))

    def _update_visual_context(self, state=None):
        player = object_node((state or {}).get('player'))
        flashed = number(object_node(player.get('state')).get('flashed'))
        confirmed = self.engine.bomb_state in ('planted', 'defusing')
        received = self._last_gsi if self._last_gsi is not None else time.monotonic() - 4
        learned_after = self.engine.server_plant_received_at
        self.visual_monitor.set_context(
            (self.engine.map_name, self.engine.round_number), self.engine.phase, confirmed, received,
            learn_after=learned_after + .5 if learned_after is not None else None,
            blocked=bool(flashed and flashed > 0),
            team_pending=bool(self.engine.map_name and not self.engine.team_selection_done))

    def _on_visual_bomb(self, observed_at):
        if self._closed or not self.enabled:
            return
        cue = self.engine.confirm_bomb_visual(observed_at)
        if cue:
            self.diagnostic.emit('music_bomb_visual', {'capture_to_ui_ms': round((time.monotonic() - observed_at) * 1000, 1)})
            self._update_visual_context()
            self._update_clock_context()
            self._update_tick_rate()
            if not self.previewing and not self._team_menu_open:
                self._play_cue(cue)

    def set_volume(self, value):
        self.settings['volume'] = max(0, min(100, int(value)))
        self.player.set_volume(self.settings['volume'])
        self._queue_save()

    def set_timing(self, estimate, bomb_seconds, round_seconds=None):
        # The optional legacy round argument is ignored; only game time counts.
        self.settings.update(estimate_timers=bool(estimate), bomb_seconds=int(bomb_seconds))
        self.engine.estimate_timers = bool(estimate)
        self.engine.bomb_seconds = int(bomb_seconds)
        # Settings apply to the next observed phase, not an arbitrary point in it.
        if not estimate and self.engine.bomb_estimated:
            self.engine.bomb_deadline = None
        self._queue_save()

    def set_warning_lead(self, milliseconds):
        value = max(0, min(2000, int(milliseconds)))
        self.settings['c4_warning_lead_ms'] = self.engine.warning_lead_ms = value
        self._queue_save()

    def process_gsi(self, state):
        if self._closed:
            return
        first = self._last_gsi is None
        self._last_gsi = time.monotonic()
        # Preserve only whether the old round replaced MVP with its C4 song;
        # processing the first new preparation packet clears that old scope.
        old_t_override = self._t_bomb_override_round
        old_t_exploded = self._t_bomb_exploded
        cue = self.engine.process(state)
        preparation = None
        if cue and cue.event == 'mvp' and self.engine.phase == 'freezetime':
            preparation = MusicCue('freeze_time', cue.started)
            self.engine.current_cue = preparation
            if old_t_override and old_t_exploded and old_t_override[0] == self.engine.map_name:
                cue = preparation
        self._sync_t_bomb_override_round()
        if self._t_bomb_override_round is not None:
            # The round summary can confirm the explosion before a detailed
            # bomb node catches up. Retain it for a later sparse MVP packet.
            self._t_bomb_exploded |= (object_node(state.get('round')).get('bomb') == 'exploded'
                                      or object_node(state.get('bomb')).get('state') == 'exploded')
        map_info = object_node(state.get('map'))
        player = object_node(state.get('player'))
        if 'phase' in map_info:
            self._clock_map_live = map_info['phase'] == 'live'
        self._clock_menu = player.get('activity') == 'menu'
        self._clock_blocked = bool(number(object_node(player.get('state')).get('flashed')))
        if self._clock_menu and self.engine.round_clock_source == 'hud':
            self.engine.round_deadline = None
            self.engine.round_clock_source = ''
        self._update_clock_context()
        self._update_round_time()
        self._update_visual_context(state)
        player = object_node(state.get('player'))
        map_info = object_node(state.get('map'))
        observing_other = bool(self.engine.local_id and player.get('steamid')
                               and str(player['steamid']) != self.engine.local_id)
        mvp_signature = (self.engine.mvp_source, self.engine.mvps, observing_other, self.engine.phase)
        if mvp_signature != self._mvp_signature:
            self._mvp_signature = mvp_signature
            self.diagnostic.emit('music_mvp_state', {
                'source': self.engine.mvp_source or 'unavailable', 'local_mvps': self.engine.mvps,
                'observing_other': observing_other, 'round_phase': self.engine.phase,
                'triggered': bool(cue and cue.event == 'mvp')})
        team_state = (self.engine.map_name, map_info.get('phase'), self.engine.phase,
                      player.get('activity'), player.get('team'), self.engine.selecting_team,
                      self.engine.team_selection_done, map_info.get('mode'))
        if team_state != self._team_state_signature:
            self._team_state_signature = team_state
            self.diagnostic.emit('music_team_state', dict(zip(
                ('map', 'map_phase', 'round_phase', 'activity', 'team', 'selecting', 'selection_done', 'mode'), team_state)))
        if self.settings['gsi_plant_delay_ms'] != self.engine.gsi_plant_delay_ms:
            self.settings['gsi_plant_delay_ms'] = self.engine.gsi_plant_delay_ms
            self._queue_save()
        round_info, bomb, countdown = (object_node(state.get(key)) for key in ('round', 'bomb', 'phase_countdowns'))
        inputs = tuple(value if isinstance(value, str) else None for value in (
            round_info.get('phase'), round_info.get('bomb'), bomb.get('state'), countdown.get('phase')))
        if inputs != self._bomb_input_signature:
            self._bomb_input_signature = inputs
            self.diagnostic.emit('music_bomb_inputs', {
                'round_phase': inputs[0], 'round_bomb': inputs[1], 'bomb_state': inputs[2], 'countdown_phase': inputs[3],
                'phase_seconds': number(countdown.get('phase_ends_in')), 'bomb_seconds': number(bomb.get('countdown'))})
        received = state.get('_toolkit_received_at', self._last_gsi)
        queue_ms = max(0, (self._last_gsi - received) * 1000) if isinstance(received, (int, float)) else None
        relation = ('self' if self.engine.planter_id == self.engine.local_id else 'other') if self.engine.planter_id else 'unknown'
        signature = (self.engine.bomb_state, relation, self.engine.planter_source, self.engine.server_bomb_planted,
                     self.engine.plant_signal_source, self.engine.bomb_estimated)
        if signature != self._bomb_signature and self.engine.bomb_state:
            self.diagnostic.emit('music_bomb_state', {'state': self.engine.bomb_state, 'owner': relation,
                'server_plant_seen': self.engine.server_bomb_planted,
                'signal_source': self.engine.plant_signal_source,
                'timer_estimated': self.engine.bomb_estimated,
                'timer_compensation_ms': self.engine.bomb_timer_compensation_ms,
                'gsi_plant_delay_ms': self.engine.gsi_plant_delay_ms,
                'owner_source': self.engine.planter_source or 'unavailable', 'ui_queue_ms': queue_ms,
                'local_anchor_age_ms': round((time.monotonic() - self.engine.local_plant_time) * 1000, 1) if self.engine.local_plant_time is not None else None,
                'evidence': dict(self.engine.plant_evidence)})
        self._bomb_signature = signature
        self._update_tick_rate()
        pause_handled = self._update_pause_music()
        if (cue and self.enabled and not self.previewing and not self._team_menu_open
                and not pause_handled and not self._pause_owns_music()):
            self._play_cue(cue)
            if preparation and cue.event == 'mvp' and self._protected_cue is not None:
                self._play_cue(preparation)
            elif preparation and cue.event == 'mvp':
                self._result_return_cue = preparation
        elif first and self.enabled and not self.previewing:
            self._status('游戏数据已连接，等待音乐事件')

    def _update_round_time(self):
        now = time.monotonic()
        deadline = self.engine.round_deadline
        remaining = (max(0, math.ceil(deadline - now))
                     if deadline is not None and self.engine.phase == 'live' and not self.engine.round_paused
                     and self._last_gsi is not None and now - self._last_gsi <= 3 else None)
        source = self.engine.round_clock_source if remaining is not None else ''
        if (remaining, source) != (self.round_seconds_remaining, self.round_time_source):
            self.round_seconds_remaining = remaining
            self.round_time_source = source
            self.round_time_changed.emit(remaining)

    def _update_tick_rate(self):
        deadline = self.engine.bomb_deadline if self.engine.bomb_state in ('planted', 'defusing') else self.engine.round_deadline
        urgent = self.enabled and deadline is not None and 0 < deadline - time.monotonic() <= 11 + self.engine.warning_lead_ms / 1000
        interval = 50 if urgent else 200
        if self.tick_timer.interval() != interval:
            self.tick_timer.setInterval(interval)

    def _audio_loaded(self):
        if self._audio_request:
            event, started, received = self._audio_request
            self._audio_request = None
            self.diagnostic.emit('music_audio_loaded', {'cue': event,
                'load_ms': round((time.monotonic() - started) * 1000, 2),
                'cue_to_loaded_ms': round((time.monotonic() - received) * 1000, 2)})

    @staticmethod
    def _clip_identity(clip):
        return (clip['path'], clip.get('start', 0), clip.get('duration', 0))

    def _choose_clip(self, event, clips):
        options = event_options(self.kit, event)
        if not options['random']:
            return dict(clips[options['selected_index']])
        key = (self.kit['id'], event)
        previous = self._last_random_clip.get(key)
        candidates = [clip for clip in clips if self._clip_identity(clip) != previous] or clips
        clip = random.choice(candidates)
        self._last_random_clip[key] = self._clip_identity(clip)
        return dict(clip)

    def _repeat_cue(self):
        if self._pause_fade_pending:
            return
        cue = self._display_cue()
        if self._closed or not self.enabled or self.previewing or not cue or not self._cue_choice or not self.kit:
            return
        key, clip = self._cue_choice
        event = key[3]
        if key[:3] != (self.kit['id'], cue.event, cue.started) or not clip.get('loop'):
            return
        clips = self.kit['tracks'].get(event, [])
        if event_options(self.kit, event)['random'] and len(clips) > 1:
            self._play_cue(cue, repeat=True)

    def _clear_playback_protection(self):
        self._protected_cue = None
        self._t_bomb_protected = False
        self._t_bomb_override_round = None
        self._t_bomb_loss_started = False
        self._t_bomb_exploded = False
        self._pending_result_cues.clear()
        self._pending_cue = None
        self._result_return_cue = None

    def _sync_t_bomb_override_round(self):
        scope = self._t_bomb_override_round
        if scope is None:
            return
        if self.engine.map_name != scope[0] or self.engine.team != 'T':
            if self._t_bomb_protected:
                self._clear_playback_protection()
            else:
                self._t_bomb_override_round = None
                self._t_bomb_loss_started = False
                self._t_bomb_exploded = False
        elif (self.engine.phase == 'freezetime'
              or (self.engine.phase == 'live' and self.engine.round_number != scope[1])):
            # An incremented round counter in an 'over' packet still belongs
            # to the old result. Reset result suppression when play restarts.
            self._t_bomb_override_round = None
            self._t_bomb_loss_started = False
            self._t_bomb_exploded = False
        elif self.engine.bomb_state == 'exploded':
            self._t_bomb_exploded = True

    def _play_cue(self, cue, resume=False, repeat=False, round_result=None):
        if round_result is None and cue.round_result:
            round_result = cue.round_result
        if cue.event == 'timeout':
            self._clear_playback_protection()
        elif self._pause_owns_music() and not self._team_menu_open:
            return
        self._sync_t_bomb_override_round()
        if (self._t_bomb_override_round is not None and self._t_bomb_exploded
                and cue.event == 'stop'):
            # A terminal bomb packet may precede the winner. Even without
            # full-clip protection, T continuation spans that round-end gap.
            return
        result = self.engine.round_result if round_result is None else round_result
        if self._t_bomb_override_round is not None and cue.event in ('round_won', 'mvp'):
            if result == 'round_lost':
                if self._t_bomb_loss_started:
                    return
                cue = MusicCue('round_lost', cue.started)
            elif self._t_bomb_exploded:
                self.diagnostic.emit('music_cue_replaced_by_t_c4', {'cue': cue.event})
                return
            else:
                # Winning by eliminating CTs must immediately use the normal
                # victory/MVP path, even if a protected warning is still playing.
                self._clear_playback_protection()
        if cue.event == 'round_lost' and (self._t_bomb_protected or self._t_bomb_override_round is not None):
            if self._t_bomb_override_round is not None and self._t_bomb_loss_started:
                return
            scope = self._t_bomb_override_round
            self._clear_playback_protection()
            self._t_bomb_override_round = scope
            self._t_bomb_loss_started = True
        if self._protected_cue is not None:
            if cue != self._protected_cue:
                pending = (cue, self.engine.round_result if round_result is None else round_result)
                if cue.event in FINISH_BEFORE_SWITCH_EVENTS:
                    if not any(item[0] == cue for item in self._pending_result_cues):
                        self._pending_result_cues.append(pending)
                else:
                    # Keep current game context, not a backlog of expired phases.
                    self._pending_cue = pending
            return
        self._audio_request = None
        if cue.event == 'stop':
            self._cue_choice = None
            self._t_bomb_protected = False
            self.player.stop()
            self._status('已进入对局，等待下一个音乐事件')
            return
        event = cue.event
        clips = self.kit['tracks'].get(event, []) if self.kit else []
        used_fallback = False
        if not clips and self.kit:
            fallback = ('bomb_ten_seconds' if cue.event in ('bomb_ten_seconds_self', 'bomb_ten_seconds_other')
                        else (self.engine.round_result if round_result is None else round_result) if cue.event == 'mvp' else '')
            event = fallback
            clips = self.kit['tracks'].get(event, [])
            used_fallback = bool(clips)
        if not clips:
            # A phase change must not leave the previous phase's loop playing.
            self.player.stop()
            self._status(SLOT_MAP[cue.event].title + '：尚未设置切片')
            return
        key = (self.kit['id'], cue.event, cue.started, event)
        if resume and self._cue_choice and self._cue_choice[0] == key:
            clip = dict(self._cue_choice[1])
        else:
            clip = self._choose_clip(event, clips)
            # Resuming after a preview keeps this event's original selection.
            self._cue_choice = (key, dict(clip))
        # A preview can change the draw history; resuming restores the audible
        # selection as the one to avoid at the next loop boundary.
        self._last_random_clip[(self.kit['id'], event)] = self._clip_identity(clip)
        clip_index = next((i for i, item in enumerate(clips) if item == clip), -1)
        if resume and not clip.get('loop'):
            elapsed = max(0, time.monotonic() - cue.started)
            if clip['duration'] > 0 and elapsed >= clip['duration']:
                self.player.stop()
                self._status('等待下一个音乐事件')
                return
            clip['start'] += elapsed
            if clip['duration'] > 0:
                clip['duration'] -= elapsed
        try:
            path = self.library.resolve(self.kit, clip)
            t_bomb = (cue.event in BOMB_WARNING_EVENTS and self.engine.team == 'T'
                      and event_options(self.kit, event)['t_round_end_override'])
            protected = (cue.event in FINISH_BEFORE_SWITCH_EVENTS
                         and event_options(self.kit, cue.event)['finish_before_switch']) or (
                         t_bomb and event_options(self.kit, event)['finish_before_switch'])
            self._t_bomb_protected = t_bomb
            if protected or t_bomb:
                clip['loop'] = False
                self._protected_cue = cue if protected else None
                if t_bomb:
                    self._t_bomb_override_round = (self.engine.map_name, self.engine.round_number)
                    self._t_bomb_loss_started = False
                    self._t_bomb_exploded = False
            if cue.event == 'team_select':
                self.player.stop()
            clip['fade_ms'] = 15 if cue.event == 'team_select' else 50 if cue.event.startswith('bomb_') else 180
            clip['seamless'] = cue.event.startswith('bomb_') or cue.event == 'round_ten_seconds'
            started = time.monotonic()
            self._audio_request = (cue.event, started, started if repeat else cue.started)
            self.diagnostic.emit('music_cue', {'cue': cue.event, 'fallback': used_fallback,
                'repeat': repeat, 'random': event_options(self.kit, event)['random'],
                't_round_end_override': t_bomb,
                'finish_before_switch': protected,
                'clip_index': clip_index,
                'clip_count': len(clips),
                'signal_source': self.engine.plant_signal_source,
                'c4_warning_lead_ms': self.engine.warning_lead_ms,
                'owner_source': self.engine.planter_source or 'unavailable'})
            if self.player.play(path, clip):
                self._pause_music_playing = cue.event == 'timeout'
                if self._pause_music_playing:
                    self._pause_was_played = True
                estimated = ((cue.event.startswith('bomb_ten_seconds') and self.engine.bomb_estimated)
                             or (cue.event == 'round_ten_seconds' and self.engine.round_estimated))
                self._status('正在播放 · ' + SLOT_MAP[cue.event].title + ('（估算计时）' if estimated else '')
                             + ('（通用兜底）' if used_fallback and cue.event.startswith('bomb_ten_seconds') else '')
                             + ('（安包者未知）' if cue.event == 'bomb_ten_seconds' else '')
                             + ' · ' + clip.get('name', path.name))
            elif ((protected and self._protected_cue == cue)
                  or (t_bomb and self._t_bomb_protected)):
                self._protected_cue = None
                self._t_bomb_protected = False
                self._t_bomb_override_round = None
                self._t_bomb_loss_started = False
                self._t_bomb_exploded = False
                self._play_deferred_cue()
        except (OSError, ValueError) as exc:
            self._failed(str(exc))

    def preview(self, event, clip=None):
        self._cancel_pause_fade()
        self._clear_playback_protection()
        self._audio_request = None
        if clip is None:
            clips = self.kit['tracks'].get(event, []) if self.kit else []
            if not clips:
                self._failed('请先为这个事件添加音频切片')
                return
            clip = self._choose_clip(event, clips)
            path = self.library.resolve(self.kit, clip)
        else:
            clip = validate_clip(clip, event)
            path = clip['path']
        clip = dict(clip)
        clip['loop'] = False
        self.previewing = True
        if self.player.play(path, clip):
            self._status('试听中 · ' + SLOT_MAP[event].title)
        else:
            self.previewing = False

    def _finished(self):
        if self._pause_fade_pending:
            self._pause_fade_finished()
            return
        if self._pause_active and self._pause_music_playing and not self.previewing:
            self._pause_music_playing = False
            self._status('暂停曲目已播放结束，等待游戏恢复')
            return
        self._t_bomb_protected = False
        if self._protected_cue is not None:
            self._protected_cue = None
        return_cue, self._result_return_cue = self._result_return_cue, None
        if self._play_deferred_cue():
            return
        if (return_cue and self.enabled and not self.previewing and not self._team_menu_open
                and not self._pause_owns_music() and self.engine.current_cue == return_cue):
            self._play_cue(return_cue)
            return
        if self.previewing:
            self.previewing = False
            if self.enabled and (self.engine.current_cue or self._team_menu_open):
                self._resume_music()
                return
        self._status('播放结束，等待下一个音乐事件')

    def _play_deferred_cue(self):
        if self._closed or not self.enabled or self.previewing:
            return False
        while self._pending_result_cues or self._pending_cue is not None:
            if self._pending_result_cues:
                cue, result = self._pending_result_cues.pop(0)
            else:
                (cue, result), self._pending_cue = self._pending_cue, None
            self._play_cue(cue, round_result=result)
            # A deferred result without protection can be superseded normally.
            if self._protected_cue is not None or not (self._pending_result_cues or self._pending_cue):
                return True
        return False

    def _failed(self, message):
        self._pause_music_playing = False
        self._protected_cue = None
        self._t_bomb_protected = False
        self._t_bomb_override_round = None
        self._t_bomb_loss_started = False
        self._t_bomb_exploded = False
        self._audio_request = None
        self.player.stop()
        self.previewing = False
        self._status('播放提示 · ' + message)
        self.error.emit(message)
        if self._pause_fade_pending:
            self._pause_fade_finished()
        else:
            self._play_deferred_cue()

    def end_preview(self):
        if self.previewing:
            self.previewing = False
            self.player.stop()
            if self.enabled and (self.engine.current_cue or self._team_menu_open):
                self._resume_music()
            else:
                self._status('试听已结束')

    def stop(self):
        self._cancel_pause_fade()
        self._clear_playback_protection()
        self._audio_request = None
        self.previewing = False
        self.player.stop()
        self._status('已停止，新的游戏事件或试听可再次开始播放')

    def _tick(self):
        self._update_round_time()
        self._update_tick_rate()
        if self._last_gsi is not None and time.monotonic() - self._last_gsi > 5:
            if (self._pause_active and self._game_window_exists is True
                    and (self._game_foreground is False or self._pause_background_hold)):
                self._pause_background_hold = True
                self._update_clock_context()
                return
            self.engine.reset()
            self._clear_pause_state()
            self._last_gsi = None
            self._update_visual_context()
            self._update_clock_context()
            if not self.previewing:
                self._clear_playback_protection()
                self.player.stop()
                self._status('游戏数据已断开，等待重新连接')
            return
        if self.enabled and not self.previewing:
            cue = self.engine.tick()
            if cue and not self._team_menu_open:
                self._play_cue(cue)

    def cleanup(self):
        if self._closed:
            return
        self._closed = True
        self._clear_pause_state()
        self._clear_playback_protection()
        self.visual_monitor.stop()
        self.clock_monitor.stop()
        self.tick_timer.stop()
        if self.save_timer.isActive():
            self.save_timer.stop()
            self._save()
        self.player.cleanup()
