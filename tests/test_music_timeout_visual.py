import threading
import time
import unittest
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter

from app.logic.music_round_clock import PauseReadings, RoundClockMonitor, pause_icon_visible, is_pause_banner, pause_banner_region
import test_music_timeout as base


def hud(text=None, pause=False, size=(144, 49)):
    image = QImage(144, 49, QImage.Format_RGB32)
    image.fill(QColor('#202020'))
    painter = QPainter(image)
    if pause:
        painter.fillRect(58, 15, 12, 30, QColor('#ff2020'))
        painter.fillRect(75, 15, 12, 30, QColor('#ff2020'))
    elif text is not None:
        font = QFont('Arial'); font.setPixelSize(32); font.setBold(True)
        painter.setFont(font); painter.setPen(QColor('#ff2020') if text.startswith('0:') else Qt.white)
        painter.drawText(image.rect(), Qt.AlignCenter, text)
    painter.end()
    return image.scaled(*size)


class PauseIconTests(unittest.TestCase):
    def test_only_active_timeout_banners_are_recognized(self):
        for text in ('T 队暂停还剩 0:14', 'CT队暂停剩余0：09', 'Timeout remaining 0:20',
                     'COUNTER-TERRORIST TIMEOUT 0:15', '技术暂停', 'MATCH PAUSED'):
            self.assertTrue(is_pause_banner(text), text)
        for text in ('请求比赛暂停', '投票失败：您的队伍已无法发起比赛暂停',
                     '发起暂停投票0:20', 'Vote for timeout 0:30', 'Timeouts remaining: 2',
                     '0:20', '准备阶段', '比赛暂停', ''):
            self.assertFalse(is_pause_banner(text), text)
        self.assertEqual(pause_banner_region((0, 0, 2560, 1440)), (920, 994, 720, 86))

    def test_pause_symbol_at_multiple_hud_resolutions(self):
        for size in ((144, 49), (108, 37), (192, 65), (72, 25)):
            self.assertIs(pause_icon_visible(hud(pause=True, size=size)), True)

    def test_normal_or_red_numeric_timer_cannot_start_pause(self):
        for text in ('1:55', '0:11', '0:01', '0:10', '0:00', '11:11'):
            self.assertIs(pause_icon_visible(hud(text)), False, text)

    def test_whiteout_blank_or_missing_frame_does_not_end_pause(self):
        for color in (Qt.white, Qt.black):
            image = QImage(144, 49, QImage.Format_RGB32); image.fill(color)
            self.assertIsNone(pause_icon_visible(image))
        self.assertIsNone(pause_icon_visible(None))
        self.assertIsNone(pause_icon_visible(QImage()))

    def test_debounce_requires_visible_confirmations_and_retains_pause_while_obscured(self):
        readings = PauseReadings()
        self.assertIsNone(readings.observe(True, 0))
        self.assertIs(readings.observe(True, .25), True)
        self.assertIsNone(readings.observe(None, 1))
        self.assertTrue(readings.paused)
        self.assertIsNone(readings.observe(False, 2))
        self.assertIsNone(readings.observe(True, 2.25))
        self.assertIsNone(readings.observe(False, 3))
        self.assertIsNone(readings.observe(False, 3.25))
        self.assertIs(readings.observe(False, 3.5), False)


class HudPauseMusicTests(unittest.TestCase):
    setUp = base.TimeoutMusicTests.setUp
    prepare = base.TimeoutMusicTests.prepare
    packet = base.TimeoutMusicTests.packet
    assign = base.TimeoutMusicTests.assign
    names = base.TimeoutMusicTests.names

    def visual(self, paused, key=None, age=0):
        key = key or (self.controller.engine.map_name, self.controller.engine.round_number)
        self.controller.clock_monitor.pause_sampled.emit(key, paused, time.monotonic()-age)

    def test_normal_player_pause_plays_and_fades_without_spectator_fields(self):
        self.prepare(); self.visual(True)
        self.assertEqual(self.names(), ['freeze_time', 'timeout'])
        self.packet()
        self.assertTrue(self.controller.engine.round_paused)
        self.assertEqual(self.controller.engine.pause_source, 'hud')
        self.visual(False)
        self.assertEqual(self.player.fade_requests, [1200])
        self.player.faded_out.emit()
        self.assertEqual(self.names(), ['freeze_time', 'timeout', 'freeze_time'])

    def test_live_round_ends_hud_pause_even_if_final_icon_disappearance_was_missed(self):
        self.prepare(); self.visual(True); self.packet(phase='live')
        self.assertFalse(self.controller.engine.round_paused)
        self.assertEqual(self.player.fade_requests, [1200])
        self.player.faded_out.emit()
        self.assertEqual(self.names()[-1], 'round_start')

    def test_stale_foreign_obscured_and_esc_callbacks_do_not_start_music(self):
        self.prepare()
        self.visual(True, age=2)
        self.visual(True, key=('other_map', 1))
        self.packet(activity='menu'); self.visual(True)
        self.packet(activity='playing')
        self.controller._clock_blocked = True; self.visual(True)
        self.controller._clock_blocked = False
        self.assertEqual(self.names(), ['freeze_time'])

    def test_explicit_gsi_phase_has_priority_over_visual_samples(self):
        self.prepare(); self.packet('freezetime'); self.visual(True)
        self.assertFalse(self.controller.engine.round_paused)
        self.packet('timeout_t'); self.visual(False)
        self.assertTrue(self.controller.engine.round_paused)
        self.assertEqual(self.controller.engine.pause_source, 'gsi')

    def test_alternate_explicit_gsi_phase_fields_and_resume(self):
        self.prepare()
        for field in ('round', 'map'):
            with self.subTest(field=field):
                self.packet('freezetime')
                data = {field: {'phase': 'paused'}}
                self.controller.process_gsi(data)
                self.assertTrue(self.controller.engine.round_paused)
                self.assertEqual(self.controller.engine.pause_field, field)
                self.assertEqual(self.names()[-1], 'timeout')
                self.controller.process_gsi({field: {'phase': 'freezetime' if field == 'round' else 'live'}})
                self.assertFalse(self.controller.engine.round_paused)
                self.player.faded_out.emit()

    def test_pause_watch_only_runs_with_timeout_clip_during_freezetime(self):
        self.prepare(clip=False)
        self.assertFalse(self.controller.clock_monitor.pause_context[1])
        self.assign('timeout', duration=0, loop=True); self.packet()
        self.assertTrue(self.controller.clock_monitor.pause_context[1])
        self.packet(activity='menu')
        self.assertFalse(self.controller.clock_monitor.pause_context[1])
        self.packet(phase='live')
        self.assertFalse(self.controller.clock_monitor.pause_context[1])

    def test_new_round_rejects_old_visual_confirmation(self):
        self.prepare(); old = ('de_dust2', 1)
        self.controller.engine.round_number = 2
        self.visual(True, key=old)
        self.assertFalse(self.controller.engine.round_paused)


class PauseWorkerTests(unittest.TestCase):
    def test_banner_is_independent_of_icon_and_ocr_runs_at_most_once_per_second(self):
        banner_reads, detected, evidence = [], [], []
        tracker = SimpleNamespace(find=lambda: 42, client_geometry=lambda hwnd: (0, 0, 2560, 1440),
            user32=SimpleNamespace(GetForegroundWindow=lambda: 42))
        source = SimpleNamespace(grab=lambda region: hud('0:15'), close=lambda: None)
        def read(image): banner_reads.append(time.monotonic()); return 'T队暂停还剩 0:14'
        reader = SimpleNamespace(read=read, close=lambda: None)
        monitor = RoundClockMonitor(source_factory=lambda: source, tracker_factory=lambda: tracker,
            pause_reader_factory=lambda: reader)
        monitor.pause_sampled.connect(lambda *args: detected.append(args))
        monitor.pause_diagnostic.connect(lambda details: evidence.append(details))
        key = ('map', 1)
        monitor.set_context(key, False, time.monotonic())
        monitor.set_pause_context(key, True, time.monotonic())
        monitor.start()
        try:
            base.pump(1450)
            self.assertTrue(any(row[1] for row in detected))
            self.assertLessEqual(len(banner_reads), 2)
            self.assertEqual(evidence[0]['source'], 'hud_banner')
        finally:
            self.assertTrue(monitor.stop())

    def test_missing_banner_ocr_does_not_disable_icon_detection(self):
        detected, phase = [], [False]
        tracker = SimpleNamespace(find=lambda: 42, client_geometry=lambda hwnd: (0, 0, 2560, 1440),
            user32=SimpleNamespace(GetForegroundWindow=lambda: 42))
        source = SimpleNamespace(grab=lambda region: hud(pause=True) if phase[0] else hud('0:15'), close=lambda: None)
        def broken_reader(): raise OSError('OCR unavailable')
        monitor = RoundClockMonitor(source_factory=lambda: source, tracker_factory=lambda: tracker,
            pause_reader_factory=broken_reader)
        monitor.pause_sampled.connect(lambda *args: detected.append(args))
        key = ('map', 1)
        monitor.set_context(key, False, time.monotonic())
        monitor.set_pause_context(key, True, time.monotonic())
        monitor.start()
        try:
            base.pump(150); phase[0] = True; base.pump(850)
            self.assertTrue(any(row[1] for row in detected))
        finally:
            self.assertTrue(monitor.stop())

    def test_pause_detection_uses_existing_capture_worker_without_ocr_and_releases_on_disable(self):
        captures, closed, detected = [], [], []
        tracker = SimpleNamespace(find=lambda: 42, client_geometry=lambda hwnd: (0, 0, 2560, 1440),
            user32=SimpleNamespace(GetForegroundWindow=lambda: 42))
        image = hud(pause=True)
        def grab(region): captures.append(region); return image
        def no_ocr(): raise AssertionError('pause detection must not initialize OCR')
        source = SimpleNamespace(grab=grab, close=lambda: closed.append(True))
        monitor = RoundClockMonitor(reader_factory=no_ocr, source_factory=lambda: source, tracker_factory=lambda: tracker)
        monitor.pause_sampled.connect(lambda *args: detected.append(args))
        key = ('map', 1)
        monitor.set_context(key, False, time.monotonic())
        monitor.set_pause_context(key, True, time.monotonic())
        monitor.start()
        try:
            base.pump(700)
            self.assertTrue(detected)
            self.assertTrue(all(r[1] for r in detected))
            self.assertLessEqual(len(captures), 3)
            monitor.set_pause_context(key, False, time.monotonic())
            base.pump(200); count = len(captures); base.pump(300)
            self.assertEqual(len(captures), count)
            self.assertTrue(closed)
        finally:
            self.assertTrue(monitor.stop())
        self.assertFalse(any(t.name == 'CS2Toolkit-RoundClock' and t.is_alive() for t in threading.enumerate()))

    def test_missing_ocr_does_not_disable_later_pause_icon_detection(self):
        seen = []
        tracker = SimpleNamespace(find=lambda: 42, client_geometry=lambda hwnd: (0, 0, 2560, 1440),
            user32=SimpleNamespace(GetForegroundWindow=lambda: 42))
        source = SimpleNamespace(grab=lambda region: hud(pause=True), close=lambda: None)
        def broken_ocr(): raise OSError('OCR language unavailable')
        monitor = RoundClockMonitor(reader_factory=broken_ocr, source_factory=lambda: source, tracker_factory=lambda: tracker)
        monitor.pause_sampled.connect(lambda *args: seen.append(args))
        key = ('map', 1)
        monitor.set_context(key, True, time.monotonic())
        monitor.start()
        try:
            base.pump(150)
            monitor.set_context(key, False, time.monotonic())
            monitor.set_pause_context(key, True, time.monotonic())
            base.pump(1100)
            self.assertTrue(any(row[1] for row in seen))
        finally:
            self.assertTrue(monitor.stop())


if __name__ == '__main__': unittest.main()
