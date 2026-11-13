"""Optional C4 HUD confirmation from a tiny foreground-only screen region."""
import base64
import ctypes
import json
from pathlib import Path
import threading
import time

import numpy as np
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QImage

from .flash_background import BufferedDesktopFrameSource
from .game_window import GameWindowTracker

MASK_SIZE = (128, 48)


def bomb_icon_region(geometry):
    x, y, width, height = geometry
    h = max(28, round(height * .04))
    w = round(h * 2.4)
    return x + width // 2 - w // 2, y, w, h


def red_icon_mask(image):
    image = image.scaled(*MASK_SIZE, Qt.IgnoreAspectRatio, Qt.SmoothTransformation).convertToFormat(QImage.Format_RGBA8888)
    pixels = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(48, image.bytesPerLine())[:, :512].reshape(48, 128, 4)
    rgb = pixels[:, :, :3].astype(np.int16)
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    return (r >= 70) & (r > g + 35) & (r > b + 25)


class BombIconMatcher:
    def __init__(self, template=None):
        self.template = template
        self.context = None
        self.armed = self.fired = False
        self.hits = self.misses = 0

    def update_context(self, key, phase, confirmed):
        context = (key, phase in ('over', 'freezetime', ''))
        if self.context != context:
            self.armed = self.fired = False
            self.hits = self.misses = 0
            self.context = context
        if confirmed:
            self.fired = True

    def lose_focus(self):
        self.armed = False
        self.hits = self.misses = 0

    def learn(self, image):
        mask = red_icon_mask(image)
        if not 20 <= int(mask.sum()) <= 1400:
            return False
        self.template = mask.copy()
        self.fired = True  # Learning follows GSI; it must not replay that cue.
        return True

    def check(self, image):
        if self.template is None or self.fired:
            return False
        mask = red_icon_mask(image)
        overlap = int(np.logical_and(mask, self.template).sum())
        match = (overlap / max(1, int(self.template.sum())) >= .80
                 and overlap / max(1, int(mask.sum())) >= .72)
        if not match:
            self.hits = 0
            self.misses += 1
            if self.misses >= 2:
                self.armed = True
        else:
            self.misses = 0
            self.hits += 1
            if self.armed and self.hits >= 2:
                self.fired = True
                return True
        return False


def load_icon_template(path):
    try:
        path = Path(path)
        if path.stat().st_size > 4096:
            return None
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('version') != 1:
            return None
        packed = base64.b64decode(data['mask'], validate=True)
        if len(packed) != 768:
            return None
        mask = np.unpackbits(np.frombuffer(packed, dtype=np.uint8)).reshape(48, 128).astype(np.bool_)
        return mask if 20 <= int(mask.sum()) <= 1400 else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_icon_template(path, template):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {'version': 1, 'mask': base64.b64encode(np.packbits(template).tobytes()).decode('ascii')}
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data), encoding='utf-8')
    temporary.replace(path)


class BombVisualMonitor(QObject):
    detected = Signal(float)
    status_changed = Signal(str)

    def __init__(self, work_dir, parent=None, source_factory=None, tracker_factory=None):
        super().__init__(parent)
        self.template_path = Path(work_dir) / 'music_box' / 'c4_bomb_icon.json'
        self.source_factory = source_factory or BufferedDesktopFrameSource
        self.tracker_factory = tracker_factory or GameWindowTracker
        self._lock = threading.Lock()
        self._context = None
        self._stop = threading.Event()
        self._thread = None
        self.status = '未开启'

    def _status(self, text):
        if self.status != text:
            self.status = text
            self.status_changed.emit(text)

    def set_context(self, key, phase, confirmed, received_at, learn_after=None, blocked=False):
        with self._lock:
            self._context = (key, phase, confirmed, received_at, learn_after, blocked)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='CS2Toolkit-C4-HUD', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(1)
            if self._thread.is_alive():
                return False
        self._thread = None
        self._status('未开启')
        return True

    def reset_template(self):
        if not self.stop():
            return False
        self.template_path.unlink(missing_ok=True)
        self._status('待学习：确认一次安包后自动学习图标')
        return True

    def _run(self):
        source = None
        try:
            # Set DPI awareness on this worker, preserving the UI thread's mode.
            try:
                dpi = ctypes.windll.user32.SetThreadDpiAwarenessContext
                dpi.argtypes, dpi.restype = [ctypes.c_void_p], ctypes.c_void_p
                dpi(ctypes.c_void_p(-4))
            except (AttributeError, OSError):
                pass
            tracker = self.tracker_factory()
            matcher = BombIconMatcher(load_icon_template(self.template_path))
            hwnd, last_find = None, 0
            while not self._stop.is_set():
                tick = time.monotonic()
                with self._lock:
                    context = self._context
                active = False
                if context:
                    key, phase, confirmed, received_at, learn_after, blocked = context
                    matcher.update_context(key, phase, confirmed)
                    active = phase == 'live' and tick - received_at <= 3 and not blocked
                    active = active and not (confirmed and matcher.template is not None)
                    if matcher.template is None:
                        active = active and confirmed and learn_after is not None and tick >= learn_after
                if active:
                    if tick - last_find >= 2:
                        hwnd, last_find = tracker.find(), tick
                    active = bool(hwnd and tracker.user32.GetForegroundWindow() == hwnd)
                if not active:
                    matcher.lose_focus()
                    if source is not None:
                        source.close()
                        source = None
                    self._status('已学习，等待游戏前台或下一回合' if matcher.template is not None
                                 else '待学习：确认一次安包后自动学习图标')
                else:
                    geometry = tracker.client_geometry(hwnd)
                    if geometry:
                        if source is None:
                            source = self.source_factory()
                        observed_at = time.monotonic()
                        image = source.grab(bomb_icon_region(geometry))
                        if image is not None and not image.isNull():
                            if matcher.template is None:
                                if confirmed and learn_after is not None and tick >= learn_after and matcher.learn(image):
                                    save_icon_template(self.template_path, matcher.template)
                                    self._status('图标已学习，下一回合自动识别')
                            else:
                                self._status('已学习，正在观察顶部 C4 图标')
                                if matcher.check(image):
                                    self.detected.emit(observed_at)
                self._stop.wait(max(.001, 1 / 6 - (time.monotonic() - tick)))
        except Exception:
            self._status('图标识别不可用，当前使用 GSI')
        finally:
            if source is not None:
                source.close()
