"""Bridge the shared GSI stream, kit storage and the dedicated Qt music player."""
import random
import time

from PySide6.QtCore import QObject, QTimer, Signal

from .music_box_events import MusicEventEngine, SLOT_MAP
from .music_box_library import MusicKitLibrary, validate_clip
from .music_box_player import MusicBoxPlayer

DEFAULT_SETTINGS = {'enabled': False, 'volume': 50, 'selected_kit': '',
                    'estimate_timers': True, 'bomb_seconds': 40, 'round_seconds': 115}


class MusicBoxController(QObject):
    changed = Signal()
    status_changed = Signal(str)
    error = Signal(str)

    def __init__(self, config_manager, parent=None, player=None):
        super().__init__(parent)
        self.config = config_manager
        self.library = MusicKitLibrary(config_manager.work_dir)
        self.settings = dict(DEFAULT_SETTINGS)
        saved = config_manager.get('music_box', {})
        if isinstance(saved, dict):
            self.settings.update({key: saved[key] for key in DEFAULT_SETTINGS if key in saved})
        for key, low, high in (('volume', 0, 100), ('bomb_seconds', 11, 180), ('round_seconds', 11, 3600)):
            try:
                self.settings[key] = max(low, min(high, int(self.settings[key])))
            except (TypeError, ValueError, OverflowError):
                self.settings[key] = DEFAULT_SETTINGS[key]
        self.engine = MusicEventEngine(estimate_timers=self.settings['estimate_timers'],
                                       bomb_seconds=self.settings['bomb_seconds'], round_seconds=self.settings['round_seconds'])
        self.player = player or MusicBoxPlayer(self)
        self.player.set_volume(self.settings['volume'])
        self.player.failed.connect(self._failed)
        self.player.finished.connect(self._finished)
        self.kit = None
        self.previewing = False
        self.status = '未启用，准备好音乐切片后即可配置'
        self._last_gsi = None
        self._closed = False
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.timeout.connect(self._save)
        self.tick_timer = QTimer(self)
        self.tick_timer.timeout.connect(self._tick)
        self.tick_timer.start(200)
        self.reload_library()
        if self.enabled:
            self._status('已启用，等待游戏数据')

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
        self.changed.emit()

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

    def process_gsi(self, state):
        if self._closed:
            return
        first = self._last_gsi is None
        self._last_gsi = time.monotonic()
        cue = self.engine.process(state)
        if cue and self.enabled and not self.previewing:
            self._play_cue(cue)
        elif first and self.enabled and not self.previewing:
            self._status('游戏数据已连接，等待音乐事件')

    def _play_cue(self, cue, resume=False):
        if cue.event == 'stop':
            self.player.stop()
            self._status('已进入对局，等待下一个音乐事件')
            return
        clips = self.kit['tracks'].get(cue.event, []) if self.kit else []
        if not clips and self.kit:
            fallback = ('bomb_ten_seconds' if cue.event in ('bomb_ten_seconds_self', 'bomb_ten_seconds_other')
                        else self.engine.round_result if cue.event == 'mvp' else '')
            clips = self.kit['tracks'].get(fallback, [])
        if not clips:
            # A phase change must not leave the previous phase's loop playing.
            self.player.stop()
            self._status(SLOT_MAP[cue.event].title + '：尚未设置切片')
            return
        clip = dict(random.choice(clips))
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
            if self.player.play(path, clip):
                estimated = ((cue.event.startswith('bomb_ten_seconds') and self.engine.bomb_estimated)
                             or (cue.event == 'round_ten_seconds' and self.engine.round_estimated))
                self._status('正在播放 · ' + SLOT_MAP[cue.event].title + ('（估算计时）' if estimated else '')
                             + ' · ' + clip.get('name', path.name))
        except (OSError, ValueError) as exc:
            self._failed(str(exc))

    def preview(self, event, clip=None):
        if clip is None:
            clips = self.kit['tracks'].get(event, []) if self.kit else []
            if not clips:
                self._failed('请先为这个事件添加音频切片')
                return
            clip = random.choice(clips)
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
        self.previewing = False
        self.player.stop()
        self._status('已停止，新的游戏事件或试听可再次开始播放')

    def _tick(self):
        if self._last_gsi is not None and time.monotonic() - self._last_gsi > 5:
            self.engine.reset()
            self._last_gsi = None
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
        self.tick_timer.stop()
        if self.save_timer.isActive():
            self.save_timer.stop()
            self._save()
        self.player.cleanup()
