"""Bridge the shared GSI stream, kit storage and the dedicated Qt music player."""
import random
import time

from PySide6.QtCore import QObject, QTimer, Signal

from .music_box_events import MusicEventEngine, SLOT_MAP, object_node, number
from .music_box_library import MusicKitLibrary, event_options, validate_clip
from .music_box_player import MusicBoxPlayer
from .music_bomb_visual import BombVisualMonitor

DEFAULT_SETTINGS = {'enabled': False, 'volume': 50, 'selected_kit': '',
                    'estimate_timers': True, 'bomb_seconds': 40, 'round_seconds': 115, 'c4_warning_lead_ms': 500,
                    'gsi_plant_delay_ms': 0, 'c4_visual_enabled': False}


class MusicBoxController(QObject):
    changed = Signal()
    status_changed = Signal(str)
    error = Signal(str)
    diagnostic = Signal(str, dict)
    visual_status_changed = Signal(str)

    def __init__(self, config_manager, parent=None, player=None):
        super().__init__(parent)
        self.config = config_manager
        self.library = MusicKitLibrary(config_manager.work_dir)
        self.settings = dict(DEFAULT_SETTINGS)
        saved = config_manager.get('music_box', {})
        if isinstance(saved, dict):
            self.settings.update({key: saved[key] for key in DEFAULT_SETTINGS if key in saved})
        self.settings['c4_visual_enabled'] = self.settings['c4_visual_enabled'] is True
        for key, low, high in (('volume', 0, 100), ('bomb_seconds', 11, 180), ('round_seconds', 11, 3600),
                               ('c4_warning_lead_ms', 0, 2000), ('gsi_plant_delay_ms', 0, 3000)):
            try:
                self.settings[key] = max(low, min(high, int(self.settings[key])))
            except (TypeError, ValueError, OverflowError):
                self.settings[key] = DEFAULT_SETTINGS[key]
        self.engine = MusicEventEngine(estimate_timers=self.settings['estimate_timers'],
                                       bomb_seconds=self.settings['bomb_seconds'], round_seconds=self.settings['round_seconds'],
                                       warning_lead_ms=self.settings['c4_warning_lead_ms'],
                                       gsi_plant_delay_ms=self.settings['gsi_plant_delay_ms'])
        self.player = player or MusicBoxPlayer(self)
        self.player.set_volume(self.settings['volume'])
        self.player.failed.connect(self._failed)
        self.player.finished.connect(self._finished)
        if hasattr(self.player, 'started'):
            self.player.started.connect(self._audio_loaded)
        self._audio_request = None
        self._cue_choice = None
        self._bomb_signature = None
        self._bomb_input_signature = None
        self.kit = None
        self.previewing = False
        self.status = '未启用，准备好音乐切片后即可配置'
        self._last_gsi = None
        self._closed = False
        self.visual_monitor = BombVisualMonitor(config_manager.work_dir, self)
        self.visual_monitor.detected.connect(self._on_visual_bomb)
        self.visual_monitor.status_changed.connect(self.visual_status_changed)
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
        if self.enabled and self.engine.current_cue:
            self._play_cue(self.engine.current_cue, resume=True)
        self.changed.emit()

    def set_enabled(self, enabled):
        self.settings['enabled'] = bool(enabled)
        self._queue_save()
        if not enabled:
            self.stop()
            self._status('创意音乐盒已关闭')
        else:
            self._status('已启用，等待游戏事件' if self.kit else '请先创建或导入一个音乐盒')
            if self.engine.current_cue:
                self._play_cue(self.engine.current_cue, resume=True)
        self._sync_visual_monitor()
        self.changed.emit()

    def _sync_visual_monitor(self):
        if self.enabled and self.settings['c4_visual_enabled'] and not self._closed:
            self.visual_monitor.start()
        else:
            self.visual_monitor.stop()

    def set_visual_enabled(self, enabled):
        self.settings['c4_visual_enabled'] = bool(enabled)
        self._queue_save()
        self._sync_visual_monitor()
        self.changed.emit()

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
            blocked=bool(flashed and flashed > 0))

    def _on_visual_bomb(self, observed_at):
        if self._closed or not self.enabled or not self.settings['c4_visual_enabled']:
            return
        cue = self.engine.confirm_bomb_visual(observed_at)
        if cue:
            self.diagnostic.emit('music_bomb_visual', {'capture_to_ui_ms': round((time.monotonic() - observed_at) * 1000, 1)})
            self._update_visual_context()
            self._update_tick_rate()
            if not self.previewing:
                self._play_cue(cue)

    def set_volume(self, value):
        self.settings['volume'] = max(0, min(100, int(value)))
        self.player.set_volume(self.settings['volume'])
        self._queue_save()

    def set_timing(self, estimate, bomb_seconds, round_seconds):
        self.settings.update(estimate_timers=bool(estimate), bomb_seconds=int(bomb_seconds), round_seconds=int(round_seconds))
        self.engine.estimate_timers = bool(estimate)
        self.engine.bomb_seconds = int(bomb_seconds)
        self.engine.round_seconds = int(round_seconds)
        # Settings apply to the next observed phase, not an arbitrary point in it.
        if not estimate and self.engine.round_estimated:
            self.engine.round_deadline = None
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
        cue = self.engine.process(state)
        self._update_visual_context(state)
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
        if cue and self.enabled and not self.previewing:
            self._play_cue(cue)
        elif first and self.enabled and not self.previewing:
            self._status('游戏数据已连接，等待音乐事件')

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

    def _choose_clip(self, event, clips):
        options = event_options(self.kit, event)
        return dict(random.choice(clips) if options['random'] else clips[options['selected_index']])

    def _play_cue(self, cue, resume=False):
        self._audio_request = None
        if cue.event == 'stop':
            self._cue_choice = None
            self.player.stop()
            self._status('已进入对局，等待下一个音乐事件')
            return
        event = cue.event
        clips = self.kit['tracks'].get(event, []) if self.kit else []
        used_fallback = False
        if not clips and self.kit:
            fallback = ('bomb_ten_seconds' if cue.event in ('bomb_ten_seconds_self', 'bomb_ten_seconds_other')
                        else self.engine.round_result if cue.event == 'mvp' else '')
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
            clip['fade_ms'] = 50 if cue.event.startswith('bomb_') else 180
            self._audio_request = (cue.event, time.monotonic(), cue.started)
            self.diagnostic.emit('music_cue', {'cue': cue.event, 'fallback': used_fallback,
                'signal_source': self.engine.plant_signal_source,
                'c4_warning_lead_ms': self.engine.warning_lead_ms,
                'owner_source': self.engine.planter_source or 'unavailable'})
            if self.player.play(path, clip):
                estimated = ((cue.event.startswith('bomb_ten_seconds') and self.engine.bomb_estimated)
                             or (cue.event == 'round_ten_seconds' and self.engine.round_estimated))
                self._status('正在播放 · ' + SLOT_MAP[cue.event].title + ('（估算计时）' if estimated else '')
                             + ('（通用兜底）' if used_fallback and cue.event.startswith('bomb_ten_seconds') else '')
                             + ('（安包者未知）' if cue.event == 'bomb_ten_seconds' else '')
                             + ' · ' + clip.get('name', path.name))
        except (OSError, ValueError) as exc:
            self._failed(str(exc))

    def preview(self, event, clip=None):
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
        if self.previewing:
            self.previewing = False
            if self.enabled and self.engine.current_cue:
                self._play_cue(self.engine.current_cue, resume=True)
                return
        self._status('播放结束，等待下一个音乐事件')

    def _failed(self, message):
        self._audio_request = None
        self.player.stop()
        self.previewing = False
        self._status('播放提示 · ' + message)
        self.error.emit(message)

    def end_preview(self):
        if self.previewing:
            self.previewing = False
            self.player.stop()
            if self.enabled and self.engine.current_cue:
                self._play_cue(self.engine.current_cue, resume=True)
            else:
                self._status('试听已结束')

    def stop(self):
        self._audio_request = None
        self.previewing = False
        self.player.stop()
        self._status('已停止，新的游戏事件或试听可再次开始播放')

    def _tick(self):
        self._update_tick_rate()
        if self._last_gsi is not None and time.monotonic() - self._last_gsi > 5:
            self.engine.reset()
            self._last_gsi = None
            self._update_visual_context()
            if not self.previewing:
                self.player.stop()
                self._status('游戏数据已断开，等待重新连接')
            return
        if self.enabled and not self.previewing:
            cue = self.engine.tick()
            if cue:
                self._play_cue(cue)

    def cleanup(self):
        if self._closed:
            return
        self._closed = True
        self.visual_monitor.stop()
        self.tick_timer.stop()
        if self.save_timer.isActive():
            self.save_timer.stop()
            self._save()
        self.player.cleanup()
