import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import copy
from pathlib import Path
import struct
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QImage, QImageReader, QMovie
from PySide6.QtWidgets import QApplication, QPushButton, QWidget
from shiboken6 import isValid

from app.logic.visual_handler import OverlayWindow, VisualHandler
from app.ui.pages.visual_page import KillIconDialog

APP = QApplication.instance() or QApplication([])


def pump(milliseconds):
    end = time.monotonic()+milliseconds/1000
    while time.monotonic() < end:
        APP.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        time.sleep(.005)


def two_frame_gif(loop=None):
    """Hand-built 4x4 GIF: transparent border, red then green centre."""
    result = b'GIF89a'+struct.pack('<HHBBB', 4, 4, 0x81, 0, 0)
    result += bytes((0, 0, 0, 255, 0, 0, 0, 255, 0, 0, 0, 255))
    if loop is not None:
        result += b'\x21\xff\x0bNETSCAPE2.0\x03\x01'+struct.pack('<H', loop)+b'\x00'
    for colour in (1, 2):
        result += b'\x21\xf9\x04\x09\x0c\x00\x00\x00'
        result += b'\x2c'+struct.pack('<HHHHB', 0, 0, 4, 4, 0)
        codes = []
        for y in range(4):
            for x in range(4): codes.extend((4, colour if x in (1, 2) and y in (1, 2) else 0))
        codes.append(5)
        bits = sum(code << (index*3) for index, code in enumerate(codes))
        encoded = bits.to_bytes((len(codes)*3+7)//8, 'little')
        result += bytes((2, len(encoded)))+encoded+b'\x00'
    return result+b'\x3b'


class Config:
    def __init__(self, visual=None): self.values = {'visual': copy.deepcopy(visual or {})}
    def get(self, key, default=None): return self.values.get(key, default)
    def set(self, key, value): self.values[key] = copy.deepcopy(value)


class KillIconGifTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gif = Path(self.temp.name)/'图标.GIF'
        self.gif.write_bytes(two_frame_gif())
        self.png = Path(self.temp.name)/'static.png'
        image = QImage(4, 4, QImage.Format_ARGB32); image.fill(Qt.blue); image.save(str(self.png))
        tracker = SimpleNamespace(hwnd=None, find=lambda: None, client_geometry=lambda *args: None,
                                  foreground_game=lambda: None, place_overlay=lambda *args: None)
        native = patch('app.logic.visual_handler.GameWindowTracker', return_value=tracker)
        native.start(); self.addCleanup(native.stop)
        logging = patch('app.logic.visual_handler.record')
        logging.start(); self.addCleanup(logging.stop)
        self.overlay = OverlayWindow()
        self.movies = []
        self.addCleanup(self.close_overlay)

    def movie(self):
        movie = self.overlay.kill_icon_label.movie()
        if movie is not None: self.movies.append(movie)
        return movie

    def close_overlay(self):
        self.overlay.focus_timer.stop(); self.overlay.geometry_timer.stop()
        self.overlay.preview_timer.stop(); self.overlay.fade_timer.stop()
        self.overlay.kill_icon_timer.stop()
        self.overlay.close(); self.overlay.deleteLater(); pump(20)
        # A failing old implementation leaks decoders and locks the fixture on
        # Windows. Clean them after assertions, without hiding the failure.
        for movie in self.movies:
            if isValid(movie): movie.stop(); movie.deleteLater()
        pump(20)

    def test_gif_plays_once_and_holds_last_frame_even_if_file_requests_looping(self):
        for loop in (None, 0, 2):
            with self.subTest(loop=loop):
                path = Path(self.temp.name)/f'once-{loop}.gif'
                path.write_bytes(two_frame_gif(loop=loop))
                self.overlay.show_kill_icon(str(path))
                movie = self.movie()
                self.assertIsNotNone(movie)
                self.assertEqual(movie.currentFrameNumber(), 0)
                image = movie.currentPixmap().toImage()
                self.assertEqual(image.pixelColor(0, 0).alpha(), 0)
                self.assertEqual(image.pixelColor(image.width()//2, image.height()//2).red(), 255)
                frames = []; movie.frameChanged.connect(frames.append)
                pump(650)
                self.assertEqual(movie.state(), QMovie.NotRunning)
                self.assertEqual(movie.currentFrameNumber(), 1)
                self.assertNotIn(0, frames, 'the first frame must not repeat after initial playback')
                image = movie.currentPixmap().toImage()
                self.assertEqual(image.pixelColor(image.width()//2, image.height()//2).green(), 255)
                self.assertFalse(self.overlay.kill_icon_label.isHidden())
                self.assertTrue(self.overlay.kill_icon_timer.isActive())

    def test_followup_kill_restarts_first_frame_and_releases_old_movie(self):
        self.overlay.show_kill_icon(str(self.gif))
        old = self.movie()
        pump(160)
        self.assertEqual(old.currentFrameNumber(), 1)
        self.overlay.show_kill_icon(str(self.gif))
        current = self.movie()
        self.assertEqual(current.currentFrameNumber(), 0)
        pump(30)
        self.assertFalse(isValid(old), 'the replaced GIF decoder must be destroyed')
        self.assertGreater(self.overlay.kill_icon_timer.remainingTime(), 2800)

    def test_switch_to_static_image_releases_gif_and_shows_new_artwork(self):
        self.overlay.show_kill_icon(str(self.gif))
        old = self.movie()
        self.overlay.show_kill_icon(str(self.png)); pump(25)
        self.assertFalse(isValid(old))
        self.assertIsNone(self.overlay.kill_icon_label.movie())
        self.assertEqual(self.overlay.kill_icon_label.pixmap().toImage().pixelColor(0, 0), Qt.blue)

    def test_auto_hide_fades_and_releases_decoder(self):
        self.overlay.show_kill_icon(str(self.gif))
        old = self.movie()
        pump(3700)
        self.assertTrue(self.overlay.kill_icon_label.isHidden())
        self.assertFalse(self.overlay.kill_icon_timer.isActive())
        self.assertFalse(isValid(old))

    def test_broken_gif_does_not_leave_empty_active_icon_or_decoder(self):
        bad = Path(self.temp.name)/'broken.gif'
        for data in (b'not a gif', b'GIF89a'):
            with self.subTest(data=data):
                bad.write_bytes(data)
                self.overlay.show_kill_icon(str(self.gif))
                old = self.movie()
                self.overlay.show_kill_icon(str(bad)); pump(30)
                self.assertTrue(self.overlay.kill_icon_label.isHidden())
                self.assertIsNone(self.overlay.kill_icon_label.movie())
                self.assertFalse(self.overlay.kill_icon_timer.isActive())
                self.assertFalse(isValid(old))

    def test_close_overlay_releases_active_decoder(self):
        self.overlay.show_kill_icon(str(self.gif))
        old = self.movie()
        self.overlay.close(); pump(30)
        self.assertFalse(isValid(old))

    def test_reported_decoder_error_stops_animation(self):
        self.overlay.show_kill_icon(str(self.gif))
        old = self.movie()
        self.assertIsNotNone(old)
        # The codec is an external event source. Exercise our response to its
        # error signal using the real running movie and real label.
        old.error.emit(QImageReader.InvalidDataError)
        pump(30)
        self.assertIsNone(self.overlay.kill_icon_label.movie())
        self.assertTrue(self.overlay.kill_icon_label.isHidden())
        self.assertFalse(self.overlay.kill_icon_timer.isActive())

    def test_game_events_select_each_gif_and_respawn_returns_to_first_icon(self):
        paths = []
        for number in range(5):
            path = Path(self.temp.name)/f'kill-{number+1}.gif'
            path.write_bytes(two_frame_gif()); paths.append(path)
        config = Config({'flash_enabled': False, 'kill_icon_enabled': True, 'kill_icon_is_advanced': True,
                         'kill_icon_path': str(self.gif), 'kill_icons_1_5': [{'path': str(path)} for path in paths]})
        handler = VisualHandler(config)
        try:
            def packet(kills, health=100, deaths=0):
                handler.process_gsi({'provider': {'steamid': 'self'},
                    'map': {'name': 'de_dust2', 'round': 1, 'phase': 'live'}, 'round': {'phase': 'live'},
                    'player': {'steamid': 'self', 'team': 'CT', 'activity': 'playing',
                               'state': {'health': health}, 'match_stats': {'kills': kills, 'deaths': deaths}}})
            packet(0)
            for kills in range(1, 6):
                packet(kills)
                movie = handler.overlay.kill_icon_label.movie()
                self.assertEqual(Path(movie.fileName()), paths[kills-1])
                self.assertEqual(movie.currentFrameNumber(), 0)
                packet(kills)
                self.assertIs(handler.overlay.kill_icon_label.movie(), movie)
            packet(5, health=0, deaths=1); packet(5, deaths=1); packet(6, deaths=1)
            self.assertEqual(Path(handler.overlay.kill_icon_label.movie().fileName()), paths[0])
        finally:
            handler.cleanup(); handler.deleteLater(); pump(30)


class KillIconPreviewTests(unittest.TestCase):
    def test_preview_draws_inside_dialog_and_fades_in_place(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'visible.gif'; path.write_bytes(two_frame_gif())
            parent = QWidget(); parent.resize(900, 900); parent.show()
            dialog = KillIconDialog(Config({'kill_icon_path': str(path)}), parent)
            try:
                dialog.show(); dialog._preview_icon(); pump(250)
                centre = dialog.preview_label.mapTo(dialog, dialog.preview_label.rect().center())
                colour = dialog.grab().toImage().pixelColor(centre)
                self.assertLess(colour.blue(), 64, 'the red/green GIF must be drawn inside its preview rectangle')
                pump(3050)
                colour = dialog.grab().toImage().pixelColor(centre)
                self.assertGreater(colour.blue(), 64)
                self.assertLess(colour.blue(), 255)
            finally:
                dialog.close(); parent.close(); dialog.deleteLater(); parent.deleteLater(); pump(30)

    def test_synchronous_decode_failure_does_not_arm_preview_timer(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'truncated.gif'; path.write_bytes(b'GIF89a')
            parent = QWidget(); parent.resize(900, 900)
            dialog = KillIconDialog(Config({'kill_icon_path': str(path)}), parent)
            try:
                dialog.show()
                with patch('app.ui.pages.visual_page.InfoBar.warning'):
                    dialog._preview_icon()
                self.assertTrue(dialog.preview_label.isHidden())
                self.assertFalse(dialog.preview_timer.isActive())
                self.assertIsNone(dialog.preview_label.movie())
            finally:
                dialog.close(); parent.close(); dialog.deleteLater(); parent.deleteLater(); pump(30)

    def test_global_and_five_kill_slots_offer_preview_and_close_releases_animation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'preview.gif'; path.write_bytes(two_frame_gif())
            paths = [Path(folder)/f'kill-{index+1}.gif' for index in range(5)]
            for item in paths: item.write_bytes(two_frame_gif())
            parent = QWidget(); parent.resize(900, 900)
            config = Config({'kill_icon_path': str(path), 'kill_icon_is_advanced': True,
                             'kill_icons_1_5': [{'path': str(item)} for item in paths]})
            dialog = KillIconDialog(config, parent)
            try:
                buttons = [button for button in dialog.findChildren(QPushButton) if '预览' in button.text()]
                self.assertEqual(len(buttons), 6, 'global and each of the five slots need a preview action')
                dialog.show()
                for button, expected_path in zip(buttons, [path]+paths):
                    button.click(); pump(30)
                    self.assertEqual(Path(dialog.preview_label.movie().fileName()), expected_path)
                preview = dialog.preview_label
                movie = preview.movie()
                self.assertIsNotNone(movie)
                self.assertEqual(Path(movie.fileName()), paths[-1])
                dialog.close(); pump(30)
                self.assertFalse(isValid(movie))
            finally:
                dialog.close(); parent.close(); dialog.deleteLater(); parent.deleteLater(); pump(30)


if __name__ == '__main__': unittest.main()
