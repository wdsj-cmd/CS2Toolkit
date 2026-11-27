import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import unittest

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter
from PySide6.QtWidgets import QApplication

from app.logic.music_pause_timer import TimerAnalyzer, PauseTimerDetector, TimerView, pause_timer_region, same_timer

APP = QApplication.instance() or QApplication([])
# The offscreen Windows plugin does not enumerate installed fonts itself.
if Path('C:/Windows/Fonts/arial.ttf').is_file():
    QFontDatabase.addApplicationFont('C:/Windows/Fonts/arial.ttf')


def timer_frame(text='1:26', resolution=(1440, 900), scale=1, red=True, bars=False):
    width, height = resolution
    _, _, w, h = pause_timer_region((0, 0, width, height))
    image = QImage(w, h, QImage.Format_RGB32)
    image.fill(QColor('#151515'))
    painter = QPainter(image)
    color = QColor('#ff2020') if red else Qt.white
    if bars:
        bh, bw, gap = max(4, round(height*.021*scale)), max(1, round(height*.007*scale)), max(1, round(height*.004*scale))
        x, y = (w-2*bw-gap)//2, max(1, round(height*.003))
        painter.fillRect(x, y, bw, bh, color)
        painter.fillRect(x+bw+gap, y, bw, bh, color)
    else:
        font = QFont('Arial'); font.setPixelSize(max(5, round(height*.025*scale))); font.setBold(True)
        painter.setFont(font); painter.setPen(color)
        painter.drawText(0, round(height*.003), w, max(10, round(height*.04)), Qt.AlignHCenter | Qt.AlignTop, text)
    painter.end()
    return image


class PauseTimerTests(unittest.TestCase):
    def test_small_pause_mark_shift_replaces_empty_cached_crop(self):
        for initial_y, shifted_y in ((5, 12), (12, 5)):
            with self.subTest(initial_y=initial_y, shifted_y=shifted_y):
                analyzer = TimerAnalyzer()
                views = []
                for y in (initial_y, shifted_y, shifted_y):
                    image = QImage(200, 96, QImage.Format_RGB32)
                    image.fill(Qt.black)
                    painter = QPainter(image)
                    for x in (96, 100):
                        painter.fillRect(x, y, 2, 5, QColor(255, 0, 0))
                    painter.end()
                    views.append(analyzer.analyze(image))
                self.assertEqual([view.kind for view in views], ['bars', 'bars', 'bars'])
                self.assertTrue(views[1].layout_changed)
                self.assertFalse(views[2].layout_changed)
                self.assertTrue(same_timer(views[0].pattern, views[1].pattern))

    RESOLUTIONS = ((720, 480), (800, 600), (1024, 768), (1280, 720), (1440, 900),
                   (1920, 1080), (3440, 1440), (3840, 2160))

    def test_relative_region_respects_client_origin_and_low_resolutions(self):
        self.assertEqual(pause_timer_region((100, 200, 720, 480)), (417, 200, 86, 48))
        for width, height in self.RESOLUTIONS:
            x, y, w, h = pause_timer_region((0, 0, width, height))
            self.assertAlmostEqual((x+w/2)/width, .5, delta=.001)
            self.assertLessEqual(w, width); self.assertLessEqual(h, height)

    def test_red_clock_and_bars_across_resolutions_and_hud_scales(self):
        for resolution in self.RESOLUTIONS:
            for scale in (.5, .7, 1):
                for bars in (False, True):
                    with self.subTest(resolution=resolution, scale=scale, bars=bars):
                        view = TimerAnalyzer().analyze(timer_frame(resolution=resolution, scale=scale, bars=bars))
                        self.assertEqual(view.kind, 'bars' if bars else 'clock')
                        self.assertTrue(view.red)

    def test_moving_red_timer_does_not_trigger_timeout(self):
        for resolution in ((720, 480), (1280, 720), (1920, 1080)):
            for scale in (.5, 1):
                analyzer, detector = TimerAnalyzer(), PauseTimerDetector()
                for step in range(32):
                    value = 45-step//2
                    result = detector.observe(analyzer.analyze(timer_frame(f'0:{value:02}', resolution, scale)), step*.5)
                    self.assertIsNone(result, (resolution, scale, step))
                    self.assertFalse(detector.paused)

    def test_each_adjacent_second_is_distinct_even_at_small_hud_scale(self):
        for resolution in ((720, 480), (1280, 720), (1440, 900), (1920, 1080)):
            for scale in (.5, 1):
                analyzer, previous = TimerAnalyzer(), None
                for second in range(60):
                    view = analyzer.analyze(timer_frame(f'1:{second:02}', resolution, scale))
                    self.assertEqual(view.kind, 'clock', (resolution, scale, second))
                    if previous is not None:
                        self.assertFalse(same_timer(previous, view.pattern), (resolution, scale, second))
                    previous = view.pattern

    def test_static_red_clock_starts_once_and_resumes_only_on_motion(self):
        analyzer, detector = TimerAnalyzer(), PauseTimerDetector()
        still = analyzer.analyze(timer_frame())
        for now in (0, .5, 1, 2, 2.5): self.assertIsNone(detector.observe(still, now))
        self.assertTrue(detector.needs_clock_verification(still, 3))
        self.assertIsNone(detector.observe(still, 3, 86))
        self.assertIs(detector.observe(still, 3.5, 86), True)
        self.assertIsNone(detector.observe(TimerView(), 4))
        self.assertTrue(detector.paused)
        # Buying, brief flash/occlusion, and disappearance of the lower banner
        # do not end the pause. A second, stable clock reading does.
        self.assertIsNone(detector.observe(still, 5))
        moved = analyzer.analyze(timer_frame('1:25', red=False))
        self.assertIsNone(detector.observe(moved, 5.5))
        self.assertIs(detector.observe(moved, 6), False)
        self.assertFalse(detector.paused)

    def test_static_white_timer_cannot_start_pause_and_noise_cannot_end_it(self):
        analyzer, detector = TimerAnalyzer(), PauseTimerDetector()
        view = analyzer.analyze(timer_frame(red=False))
        self.assertEqual(view.kind, 'clock')
        for at in range(10): self.assertIsNone(detector.observe(view, at))
        self.assertFalse(detector.paused)
        detector.seed(True)
        red = analyzer.analyze(timer_frame())
        detector.observe(red, 10)
        detector.observe(analyzer.analyze(timer_frame('1:25')), 10.5)
        detector.observe(red, 10.6)
        self.assertTrue(detector.paused)

    def test_bars_start_without_ocr_and_missing_frames_keep_pause(self):
        analyzer, detector = TimerAnalyzer(), PauseTimerDetector()
        view = analyzer.analyze(timer_frame(bars=True))
        self.assertIsNone(detector.observe(view, 0))
        self.assertIs(detector.observe(view, .5), True)
        for image in (None, QImage()):
            self.assertIsNone(detector.observe(analyzer.analyze(image), 1))
            self.assertTrue(detector.paused)

    def test_background_retains_confirmed_pause_but_cannot_accumulate_still_time(self):
        analyzer, detector = TimerAnalyzer(), PauseTimerDetector()
        view = analyzer.analyze(timer_frame())
        detector.observe(view, 0); detector.suspend()
        self.assertIsNone(detector.observe(view, 20))
        self.assertFalse(detector.paused)
        self.assertIsNone(detector.observe(view, 23, 86))
        self.assertIs(detector.observe(view, 23.5, 86), True)
        detector.suspend()
        self.assertIsNone(detector.observe(view, 50))
        self.assertTrue(detector.paused)

    def test_static_red_shapes_without_two_numeric_reads_never_start_music(self):
        analyzer, detector = TimerAnalyzer(), PauseTimerDetector()
        view = analyzer.analyze(timer_frame('10 10'))
        for at in range(30):
            self.assertIsNone(detector.observe(view, at))
        self.assertFalse(detector.paused)

    def test_changing_numeric_reads_cannot_confirm_static_picture(self):
        analyzer, detector = TimerAnalyzer(), PauseTimerDetector()
        view = analyzer.analyze(timer_frame())
        detector.observe(view, 0)
        for at, seconds in ((3, 86), (4, 85), (5, 84), (8, 84)):
            self.assertIsNone(detector.observe(view, at, seconds))
        self.assertFalse(detector.paused)

    def test_white_timer_is_not_replaced_by_a_stationary_red_score(self):
        for resolution in self.RESOLUTIONS:
            image = timer_frame('1:55', resolution, red=False)
            painter = QPainter(image)
            font = QFont('Arial'); font.setBold(True); font.setPixelSize(max(6, round(resolution[1]*.02)))
            painter.setFont(font); painter.setPen(QColor('#ff2020'))
            painter.drawText(0, round(image.height()*.5), image.width(), round(image.height()*.4),
                             Qt.AlignHCenter | Qt.AlignTop, '10 : 10')
            painter.end()
            view = TimerAnalyzer().analyze(image)
            self.assertEqual(view.kind, 'clock', resolution)
            self.assertFalse(view.red, resolution)

    def test_red_score_below_the_timer_cannot_become_a_clock(self):
        image = timer_frame()
        painter = QPainter(image); painter.fillRect(image.rect(), QColor('#151515'))
        font = QFont('Arial'); font.setPixelSize(20); font.setBold(True)
        painter.setFont(font); painter.setPen(QColor('#ff2020'))
        painter.drawText(0, 45, image.width(), 40, Qt.AlignCenter, '10 : 10')
        painter.end()
        self.assertEqual(TimerAnalyzer().analyze(image).kind, 'unknown')

    def test_white_to_red_colour_change_does_not_reuse_old_still_duration(self):
        analyzer, detector = TimerAnalyzer(), PauseTimerDetector()
        white, red = analyzer.analyze(timer_frame(red=False)), analyzer.analyze(timer_frame())
        detector.observe(white, 0); detector.observe(white, 20)
        detector.observe(red, 21)
        self.assertFalse(detector.needs_clock_verification(red, 21.5))
        self.assertTrue(detector.needs_clock_verification(red, 24))

    def test_resolution_switch_preserves_pause_and_reanchors_motion(self):
        analyzer, detector = TimerAnalyzer(), PauseTimerDetector()
        detector.seed(True)
        detector.observe(analyzer.analyze(timer_frame()), 0)
        resized = analyzer.analyze(timer_frame(resolution=(720, 480), scale=.5))
        self.assertTrue(resized.layout_changed)
        self.assertIsNone(detector.observe(resized, 1))
        self.assertTrue(detector.paused)
        moved = analyzer.analyze(timer_frame('1:25', (720, 480), .5))
        detector.observe(moved, 2)
        self.assertIs(detector.observe(moved, 2.5), False)


if __name__ == '__main__': unittest.main()
