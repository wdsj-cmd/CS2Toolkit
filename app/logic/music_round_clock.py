"""Read only the visible HUD timer when GSI supplies no round countdown."""
import asyncio
import ctypes
import math
import re
import threading
import time

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QImage

from .flash_background import BufferedDesktopFrameSource
from .game_window import GameWindowTracker


def round_clock_region(geometry):
    x, y, width, height = geometry
    size = max(64, round(height * .10))
    return x + width // 2 - size // 2, y, size, max(24, round(height * .034))


def parse_clock(text):
    match = re.fullmatch(r'([0-5]?\d):([0-5]\d)', re.sub(r'\s+', '', text).replace('：', ':'))
    return int(match[1]) * 60 + int(match[2]) if match else None


class ClockReadings:
    def __init__(self):
        self.reset()

    def reset(self):
        self.previous = self.confirmed = None
        self.misses = 0

    def observe(self, text, now):
        seconds = parse_clock(text)
        if seconds is None:
            self.misses += 1
            if self.misses >= 3:
                self.reset()
            return None
        self.misses = 0
        # Losing the minute digit must not turn 1:09 into a five-second cue.
        if self.confirmed:
            old, at = self.confirmed
            if not 0 <= old - seconds <= math.ceil(now - at) + 1:
                return None
        previous, self.previous = self.previous, (seconds, now)
        if previous:
            old, at = previous
            if .25 <= now - at <= 1.5 and old - seconds == 1:
                self.confirmed = seconds, now
                self.anchor_at = (at + now) / 2
                return seconds
        return None


class WindowsClockReader:
    """Local Windows OCR; all COM and bitmap resources stay on this worker."""
    def __init__(self):
        self._com = ctypes.WinDLL('combase')
        self._com.RoInitialize.argtypes, self._com.RoInitialize.restype = [ctypes.c_uint], ctypes.c_long
        self._com.RoUninitialize.argtypes, self._com.RoUninitialize.restype = [], None
        result = self._com.RoInitialize(1)
        self._uninitialize = result >= 0
        if result < 0 and result != -2147417850:  # An existing STA is usable too.
            raise OSError(result, 'Windows Runtime initialization failed')
        try:
            from winrt.windows.globalization import Language
            from winrt.windows.media.ocr import OcrEngine
            self.engine = OcrEngine.try_create_from_language(Language('en-US')) or OcrEngine.try_create_from_user_profile_languages()
            if self.engine is None:
                raise RuntimeError('Windows OCR language unavailable')
            self.loop = asyncio.new_event_loop()
        except Exception:
            if self._uninitialize:
                self._com.RoUninitialize()
            raise

    async def _recognize(self, image):
        from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.storage.streams import DataWriter
        scaled = image.scaled(image.width() * 4, image.height() * 4, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        scaled = scaled.convertToFormat(QImage.Format_ARGB32)
        writer = DataWriter()
        writer.write_bytes(bytes(scaled.constBits()))
        buffer = writer.detach_buffer()
        writer.close()
        bitmap = SoftwareBitmap.create_copy_with_alpha_from_buffer(buffer, BitmapPixelFormat.BGRA8,
            scaled.width(), scaled.height(), BitmapAlphaMode.IGNORE)
        try:
            result = await self.engine.recognize_async(bitmap)
            return result.text
        finally:
            bitmap.close()

    def read(self, image):
        return self.loop.run_until_complete(self._recognize(image))

    def close(self):
        self.engine = None
        self.loop.close()
        if self._uninitialize:
            self._com.RoUninitialize()


class RoundClockMonitor(QObject):
    sampled = Signal(object, int, float)
    status_changed = Signal(str)
    resync_requested = Signal(object)

    def __init__(self, parent=None, reader_factory=None, source_factory=None, tracker_factory=None):
        super().__init__(parent)
        self.reader_factory = reader_factory or WindowsClockReader
        self.source_factory = source_factory or BufferedDesktopFrameSource
        self.tracker_factory = tracker_factory or GameWindowTracker
        self._lock = threading.Lock()
        self._context = None
        self._stop = threading.Event()
        self._thread = None
        self.status = ''

    def set_context(self, key, active, received_at, watch=False):
        with self._lock:
            self._context = key, active, received_at, watch

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='CS2Toolkit-RoundClock', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(2)
            if self._thread.is_alive():
                return False
        self._thread = None
        return True

    def _status(self, text):
        if text != self.status:
            self.status = text
            self.status_changed.emit(text)

    def _run(self):
        reader = source = None
        try:
            try:
                dpi = ctypes.windll.user32.SetThreadDpiAwarenessContext
                dpi.argtypes, dpi.restype = [ctypes.c_void_p], ctypes.c_void_p
                dpi(ctypes.c_void_p(-4))
            except (AttributeError, OSError):
                pass
            tracker = self.tracker_factory()
            readings = ClockReadings()
            key = None
            was_foreground = False
            game_hwnd, last_find = None, 0
            next_sample = 0
            while not self._stop.is_set():
                now = time.monotonic()
                with self._lock:
                    context = self._context
                hwnd = None
                if context:
                    current, active, received, watch = context
                    if current != key:
                        readings.reset()
                        key = current
                        was_foreground = False
                    if (active or watch) and now - received <= 3:
                        if now - last_find >= 2:
                            game_hwnd, last_find = tracker.find(), now
                        if game_hwnd and tracker.user32.GetForegroundWindow() == game_hwnd:
                            hwnd = game_hwnd
                if hwnd and not was_foreground:
                    self.resync_requested.emit(key)
                    next_sample = 0
                was_foreground = bool(hwnd)
                if not hwnd:
                    readings.reset()
                    if source is not None:
                        source.close()
                        source = None
                    self._status('等待可见的游戏计时器')
                elif not active:
                    readings.reset()
                    if source is not None:
                        source.close()
                        source = None
                    self._status('本回合已同步，停止读取画面')
                elif now >= next_sample:
                    geometry = tracker.client_geometry(hwnd)
                    if geometry:
                        if reader is None:
                            reader = self.reader_factory()
                        if source is None:
                            source = self.source_factory()
                        observed_at = time.monotonic()
                        frame = source.grab(round_clock_region(geometry))
                        if frame is not None and not frame.isNull():
                            text = reader.read(frame)
                            if tracker.user32.GetForegroundWindow() == hwnd:
                                value = readings.observe(text, observed_at)
                                if value is not None:
                                    self.sampled.emit(key, value, readings.anchor_at)
                                    self._status('正在读取顶部计时器')
                    next_sample = time.monotonic() + (1 if readings.confirmed else .5)
                self._stop.wait(.1)
        except Exception:
            self._status('系统文字识别不可用')
        finally:
            if source is not None:
                source.close()
            if reader is not None:
                reader.close()
