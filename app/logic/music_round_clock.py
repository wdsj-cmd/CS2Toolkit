"""Read small visible HUD regions when GSI omits clock or pause state."""
import asyncio
import ctypes
import math
import re
import threading
import time

import numpy as np

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QImage

from .flash_background import BufferedDesktopFrameSource
from .game_window import GameWindowTracker
from .music_pause_timer import TimerAnalyzer, PauseTimerDetector, pause_timer_region

MAX_CLOCK_READS = 8
BANNER_RECHECK_SECONDS = 5
_PAUSE_BANNER_REJECT = re.compile(r'投票|申请|请求|失败|无法|未开始|未生效|vote|request|failed|timeoutsremaining')

def round_clock_region(geometry):
    x, y, width, height = geometry
    size = max(64, round(height * .10))
    return x + width // 2 - size // 2, y, size, max(24, round(height * .034))


def parse_clock(text):
    match = re.fullmatch(r'([0-5]?\d):([0-5]\d)', re.sub(r'\s+', '', text).replace('：', ':'))
    return int(match[1]) * 60 + int(match[2]) if match else None


def pause_banner_region(geometry):
    x, y, width, height = geometry
    size = min(width, max(240, round(height*.50)))
    return x+(width-size)//2, y+round(height*.69), size, max(32, round(height*.06))


def is_pause_banner(text):
    """Only the active-pause banner, not vote requests or menu commands."""
    compact = re.sub(r'\s+', '', str(text)).lower().replace('：', ':')
    if _PAUSE_BANNER_REJECT.search(compact):
        return False
    # At small HUD sizes OCR can read the team and "暂停还" while losing
    # "剩" or the digits. That active-team banner is already unambiguous;
    # requiring an exact remaining-time sentence discarded real timeouts.
    team_remaining = re.search(r'^(?:ct|t|反恐精英|恐怖分子)(?:队伍|队|方)?暂停(?:还|剩)', compact)
    clock = bool(re.search(r'\d{1,2}:[0-5]\d', compact))
    return bool(team_remaining or (clock and ('暂停还剩' in compact or '暂停剩余' in compact
                            or 'timeoutremaining' in compact or 'timeout' in compact))
                or compact in ('比赛已暂停', '技术暂停', 'matchpaused', 'technicaltimeout', 'technicalpause'))


def pause_banner_candidate(image):
    """Cheap text/contrast precheck before live-round OCR, never a music cue."""
    if image is None or image.isNull():
        return None
    sample = image.scaled(200, 32, Qt.IgnoreAspectRatio, Qt.FastTransformation).convertToFormat(QImage.Format_RGBA8888)
    pixels = np.frombuffer(sample.constBits(), dtype=np.uint8).reshape(32, 200, 4)[:, :, :3]
    high, low = pixels.max(axis=2), pixels.min(axis=2)
    if (high <= 20).mean() > .95 or (low >= 230).mean() > .90:
        return None
    white = (low >= 135) & (high.astype(np.int16)-low <= 90)
    rows = (white.sum(axis=1) >= 4).sum()
    return bool(white.sum() >= 20 and rows >= 3 and (high <= 155).mean() >= .15)


def pause_icon_visible(image):
    """Recognize the two solid red pause bars, never a frozen numeric timer.

    None means the HUD is obscured/unreadable; it must not end a pause.
    Normalizing only this tiny timer crop keeps HUD scales inexpensive.
    """
    if image is None or image.isNull():
        return None
    sample = image.scaled(144, 49, Qt.IgnoreAspectRatio, Qt.FastTransformation)
    # Own the converted image while numpy accesses its storage.
    rgba = sample.convertToFormat(QImage.Format_RGBA8888)
    pixels = np.frombuffer(rgba.constBits(), dtype=np.uint8).reshape(49, 144, 4)
    red = (pixels[:, :, 0] >= 170) & (pixels[:, :, 1] <= 110) & (pixels[:, :, 2] <= 110)
    if red.mean() > .30:
        return None
    remaining = set(zip(*np.nonzero(red)))
    bars = []
    while remaining:
        pending = [remaining.pop()]
        component = []
        while pending:
            y, x = pending.pop()
            component.append((y, x))
            for neighbor in ((y-1, x), (y+1, x), (y, x-1), (y, x+1)):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    pending.append(neighbor)
        if len(component) >= 12:
            ys, xs = zip(*component)
            left, top, right, bottom = min(xs), min(ys), max(xs)+1, max(ys)+1
            width, height = right-left, bottom-top
            if (not 15 <= height <= 40 or not .20 <= width/height <= .55
                    or len(component)/(width*height) < .85):
                bars = []
                break
            bars.append((left, top, right, bottom))
    if len(bars) == 2:
        a, b = sorted(bars)
        width, height = a[2]-a[0], a[3]-a[1]
        if (abs(width-(b[2]-b[0])) <= 2 and abs(height-(b[3]-b[1])) <= 2
                and abs(a[1]-b[1]) <= 2 and 1 <= b[0]-a[2] <= width
                and abs((a[0]+b[2])/2-72) <= 12):
            return True
    bright = ((pixels[:, :, :3].min(axis=2) >= 165) | red).mean()
    return False if .004 <= bright <= .45 else None


class PauseReadings:
    def __init__(self):
        self.paused = False
        self.reset_pending()

    def reset_pending(self):
        self.candidate = None
        self.count = 0
        self.since = None

    def observe(self, visible, now, end_delay=.35):
        if visible is None or visible == self.paused:
            self.reset_pending()
            return None
        if visible != self.candidate:
            self.candidate, self.count, self.since = visible, 1, now
        else:
            self.count += 1
        if self.count >= (2 if visible else 3) and now-self.since >= (.15 if visible else end_delay):
            self.paused = visible
            self.reset_pending()
            return visible
        return None


class PauseBannerTracker:
    """Track an OCR-confirmed label in memory, excluding its changing digits."""
    def __init__(self):
        self.clear()

    def clear(self):
        self.foreground = self.background = self.reference_low = self.reference_high = None

    @property
    def ready(self):
        return self.foreground is not None

    @staticmethod
    def _patch(image):
        if image is None or image.isNull():
            return None
        sample = image.scaled(200, 32, Qt.IgnoreAspectRatio, Qt.FastTransformation).convertToFormat(QImage.Format_RGBA8888)
        # The left-of-centre label is stable; the remaining-time digits are
        # deliberately outside this patch. No full-screen template is retained.
        return np.frombuffer(sample.constBits(), dtype=np.uint8).reshape(32, 200, 4)[3:27, 50:110, :3].copy()

    def remember(self, image):
        self.clear()
        pixels = self._patch(image)
        if pixels is None:
            return False
        high, low = pixels.max(axis=2), pixels.min(axis=2)
        foreground = (low >= 135) & (high.astype(np.int16)-low <= 90)
        padded = np.pad(foreground, 1)
        neighbors = np.logical_or.reduce([padded[y:y+24, x:x+60] for y in range(3) for x in range(3)])
        background = neighbors & ~foreground & (high <= 145)
        if not 10 <= foreground.sum() <= foreground.size*.4 or background.sum() < 10:
            return False
        self.foreground, self.background = foreground, background
        self.reference_low, self.reference_high = low.astype(np.int16), high.astype(np.int16)
        return True

    def matches(self, image):
        if not self.ready:
            return None
        pixels = self._patch(image)
        if pixels is None:
            return None
        high, low = pixels.max(axis=2).astype(np.int16), pixels.min(axis=2).astype(np.int16)
        if (high <= 20).mean() > .95 or (low >= 230).mean() > .95:
            return None
        # Allow one pixel of antialiasing/motion without accepting a blank
        # bright wall: both the label strokes and their dark surround must fit.
        plow, phigh = np.pad(low, 1, mode='edge'), np.pad(high, 1, mode='edge')
        light = np.maximum.reduce([plow[y:y+24, x:x+60] for y in range(3) for x in range(3)])
        dark = np.minimum.reduce([phigh[y:y+24, x:x+60] for y in range(3) for x in range(3)])
        fg = (light[self.foreground] >= np.maximum(100, self.reference_low[self.foreground]-45)).mean()
        bg = (dark[self.background] <= np.minimum(175, self.reference_high[self.background]+55)).mean()
        contrast = light[self.foreground].mean()-dark[self.background].mean()
        return bool(fg >= .8 and bg >= .7 and contrast >= 25)


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
    def __init__(self, language='en-US', scale=4):
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
            self.engine = (OcrEngine.try_create_from_language(Language(language))
                           or OcrEngine.try_create_from_user_profile_languages()
                           or OcrEngine.try_create_from_language(Language('en-US')))
            if self.engine is None:
                raise RuntimeError('Windows OCR language unavailable')
            self.loop = asyncio.new_event_loop()
            self.scale = scale
        except Exception:
            if self._uninitialize:
                self._com.RoUninitialize()
            raise

    async def _recognize(self, image, transformation=Qt.SmoothTransformation):
        from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.storage.streams import DataWriter
        scaled = image.scaled(image.width() * self.scale, image.height() * self.scale, Qt.IgnoreAspectRatio, transformation)
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

    def read(self, image, transformation=Qt.SmoothTransformation):
        return self.loop.run_until_complete(self._recognize(image, transformation))

    def read_pause_clock(self, image):
        text = self.read(image)
        if parse_clock(text) is None:
            # Only a stationary-clock candidate needs the larger retry. Keep
            # the usual round-clock reader and its sampling budget unchanged.
            scale, self.scale = self.scale, 6
            try:
                text = self.read(image)
            finally:
                self.scale = scale
        return text

    def close(self):
        self.engine = None
        self.loop.close()
        if self._uninitialize:
            self._com.RoUninitialize()


class PauseBannerReader(WindowsClockReader):
    def __init__(self):
        super().__init__(language='zh-Hans-CN', scale=2)

    def read(self, image):
        text = super().read(image)
        compact = re.sub(r'\s+', '', text).lower()
        if is_pause_banner(text) or _PAUSE_BANNER_REJECT.search(compact):
            return text
        # Preserve the small pixel-font strokes only when the first pass
        # already resembles a pause banner. Normal scenery gets no extra OCR.
        hint = ('队' in compact and ('停' in compact or '还' in compact)
                or 'timeout' in compact or 'paused' in compact)
        if hint:
            crisp = super().read(image, Qt.FastTransformation)
            if is_pause_banner(crisp):
                return crisp
        return text


class RoundClockMonitor(QObject):
    sampled = Signal(object, int, float)
    status_changed = Signal(str)
    resync_requested = Signal(object)
    pause_sampled = Signal(object, bool, float)
    pause_diagnostic = Signal(dict)
    pause_status_changed = Signal(dict)
    game_visibility_changed = Signal(bool, bool)

    def __init__(self, parent=None, reader_factory=None, source_factory=None, tracker_factory=None,
                 pause_reader_factory=None):
        super().__init__(parent)
        self.reader_factory = reader_factory or WindowsClockReader
        self.source_factory = source_factory or BufferedDesktopFrameSource
        self.tracker_factory = tracker_factory or GameWindowTracker
        self.pause_reader_factory = pause_reader_factory or PauseBannerReader
        self._lock = threading.Lock()
        self._context = None
        self._pause_context = None
        self._stop = threading.Event()
        self._thread = None
        self.status = ''
        self._next_pause_status = 0

    def set_context(self, key, active, received_at, watch=False):
        with self._lock:
            self._context = key, active, received_at, watch

    def set_pause_context(self, key, active, received_at, live_round=False, confirmed_pause=None):
        with self._lock:
            self._pause_context = key, active, received_at, live_round, confirmed_pause

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

    def _pause_status(self, stage, force=False, **details):
        # Bounded diagnostics without screenshots, OCR text, player IDs or paths.
        now = time.monotonic()
        if force or now >= self._next_pause_status:
            self._next_pause_status = now+5
            self.pause_status_changed.emit({'stage': stage, **details})

    def _run(self):
        reader = source = pause_reader = None
        try:
            try:
                dpi = ctypes.windll.user32.SetThreadDpiAwarenessContext
                dpi.argtypes, dpi.restype = [ctypes.c_void_p], ctypes.c_void_p
                dpi(ctypes.c_void_p(-4))
            except (AttributeError, OSError):
                pass
            tracker = self.tracker_factory()
            readings = ClockReadings()
            timer_analyzer = TimerAnalyzer()
            timer_detector = PauseTimerDetector()
            reported_timer_pause = None
            visibility = None
            last_pause_feedback = None
            key = None
            was_foreground = False
            was_active = False
            attempts = 0
            game_hwnd, last_find = None, 0
            next_sample = 0
            reader_unavailable = False
            pause_reader_unavailable = False
            next_banner = 0
            next_clock_verify = 0
            clock_ocr_valid = None
            banner_text_seen = banner_pause_word = banner_clock = False
            pause_ocr_error = None
            while not self._stop.is_set():
                now = time.monotonic()
                with self._lock:
                    context = self._context
                    pause_context = self._pause_context
                hwnd = None
                active = False
                pause_active = False
                if context:
                    current, active, received, watch = context
                    if current != key:
                        readings.reset()
                        timer_analyzer.reset()
                        timer_detector = PauseTimerDetector()
                        reported_timer_pause = None
                        last_pause_feedback = None
                        next_banner = 0
                        next_clock_verify = 0
                        clock_ocr_valid = None
                        banner_text_seen = banner_pause_word = banner_clock = False
                        self._next_pause_status = 0
                        key = current
                        was_foreground = False
                    pause_active = bool(pause_context and pause_context[0] == key
                                        and pause_context[1] and now-pause_context[2] <= 3)
                    feedback = pause_context[4] if pause_context else None
                    if feedback is not None:
                        if feedback != last_pause_feedback:
                            timer_detector.seed(bool(feedback))
                        if last_pause_feedback is True and feedback is False:
                            next_banner = 0
                        last_pause_feedback = feedback
                    if (active or watch or pause_active) and now - received <= 3:
                        if now - last_find >= 2:
                            game_hwnd, last_find = tracker.find(), now
                        if game_hwnd and tracker.user32.GetForegroundWindow() == game_hwnd:
                            hwnd = game_hwnd
                    if active or watch or pause_active:
                        current_visibility = bool(game_hwnd), bool(hwnd)
                        if current_visibility != visibility:
                            visibility = current_visibility
                            self.game_visibility_changed.emit(*current_visibility)
                if hwnd and not was_foreground:
                    self.resync_requested.emit(key)
                    next_sample = 0
                    attempts = 0
                if active and not was_active:
                    readings.reset()
                    attempts = 0
                was_active = active
                was_foreground = bool(hwnd)
                if not hwnd:
                    readings.reset()
                    timer_detector.suspend()
                    if pause_context and pause_context[1]:
                        stage = ('stale_gsi' if now-pause_context[2] > 3 else
                                 'waiting_foreground' if game_hwnd else 'game_window_not_found')
                        self._pause_status(stage)
                    if source is not None:
                        source.close()
                        source = None
                    self._status('等待可见的游戏计时器')
                elif not active and not pause_active:
                    readings.reset()
                    if source is not None:
                        source.close()
                        source = None
                    self._status('本回合已同步，停止读取画面')
                elif (attempts >= MAX_CLOCK_READS or reader_unavailable) and not pause_active:
                    if source is not None:
                        source.close()
                        source = None
                    self._status('未读到计时器，等待切回游戏或下一回合')
                elif now >= next_sample:
                    geometry = tracker.client_geometry(hwnd)
                    if geometry:
                        if source is None:
                            source = self.source_factory()
                        observed_at = time.monotonic()
                        clock_read = active and attempts < MAX_CLOCK_READS and not reader_unavailable
                        frame = source.grab(round_clock_region(geometry)) if clock_read else None
                        frame_valid = frame is not None and not frame.isNull()
                        if tracker.user32.GetForegroundWindow() == hwnd:
                            if pause_active:
                                timer_frame = source.grab(pause_timer_region(geometry))
                                view = timer_analyzer.analyze(timer_frame)
                                clock_seconds = None
                                if (timer_detector.needs_clock_verification(view, observed_at)
                                        and now >= next_clock_verify and not reader_unavailable):
                                    next_clock_verify = now+1
                                    try:
                                        if reader is None:
                                            reader = self.reader_factory()
                                        verify = getattr(reader, 'read_pause_clock', reader.read)
                                        clock_seconds = parse_clock(verify(view.clock_image))
                                        clock_ocr_valid = clock_seconds is not None
                                        if not clock_ocr_valid:
                                            timer_detector.verified_seconds = timer_detector.verified_at = None
                                    except Exception as error:
                                        reader_unavailable = True
                                        clock_ocr_valid = False
                                        pause_ocr_error = type(error).__name__
                                decision = timer_detector.observe(view, observed_at, clock_seconds)
                                evidence = 'timer_bars' if view.kind == 'bars' else 'timer_frozen_red_verified' if decision is True else 'timer_moving'
                                # The banner is an optional positive hint only.
                                # Its disappearance (e.g. behind the buy menu)
                                # can never stop an established pause.
                                if (decision is None and not timer_detector.paused
                                        and (view.kind == 'unknown' or not view.red)
                                        and now >= next_banner and not pause_reader_unavailable):
                                    banner_frame = source.grab(pause_banner_region(geometry))
                                    if pause_banner_candidate(banner_frame) is True:
                                        try:
                                            if pause_reader is None:
                                                pause_reader = self.pause_reader_factory()
                                            text = pause_reader.read(banner_frame)
                                            compact = re.sub(r'\s+', '', text).lower().replace('：', ':')
                                            banner_text_seen = bool(compact)
                                            banner_pause_word = '暂停' in compact or 'timeout' in compact or 'paused' in compact
                                            banner_clock = bool(re.search(r'\d{1,2}:[0-5]\d', compact))
                                            if is_pause_banner(text):
                                                timer_detector.seed(True)
                                                decision, evidence = True, 'hud_banner'
                                        except Exception as error:
                                            pause_reader_unavailable = True
                                            pause_ocr_error = type(error).__name__
                                    next_banner = time.monotonic()+1
                                if time.monotonic() >= self._next_pause_status:
                                    self._pause_status('timer_sample', window_size=list(geometry[2:]),
                                        timer_kind=view.kind, red=view.red, paused=timer_detector.paused,
                                        layout_changed=view.layout_changed,
                                        clock_ocr_valid=clock_ocr_valid,
                                        banner_text_seen=banner_text_seen, banner_pause_word=banner_pause_word,
                                        banner_clock=banner_clock, ocr_error=pause_ocr_error)
                                if decision is not None and tracker.user32.GetForegroundWindow() == hwnd:
                                    if decision != reported_timer_pause:
                                        reported_timer_pause = decision
                                        self.pause_diagnostic.emit({'active': decision, 'source': evidence})
                                    self.pause_sampled.emit(key, decision, observed_at)
                            if clock_read and frame_valid:
                                attempts += 1
                                try:
                                    if reader is None:
                                        reader = self.reader_factory()
                                    text = reader.read(frame)
                                except Exception:
                                    # Missing OCR must not disable pause detection.
                                    reader_unavailable = True
                                    self._status('系统文字识别不可用')
                                else:
                                    if tracker.user32.GetForegroundWindow() == hwnd:
                                        value = readings.observe(text, observed_at)
                                        if value is not None:
                                            self.sampled.emit(key, value, readings.anchor_at)
                                            self._status('正在读取顶部计时器')
                    elif pause_active:
                        self._pause_status('missing_geometry')
                    interval = .5 if pause_active else (1 if readings.confirmed else .5)
                    next_sample = time.monotonic()+interval
                self._stop.wait(.1)
        except Exception as error:
            if self._pause_context and self._pause_context[1]:
                self._pause_status('worker_error', force=True, error_type=type(error).__name__)
            self._status('系统文字识别不可用')
        finally:
            if source is not None:
                source.close()
            if reader is not None:
                reader.close()
            if pause_reader is not None:
                pause_reader.close()
