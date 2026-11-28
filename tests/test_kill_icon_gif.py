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

from PySide6.QtCore import QAbstractAnimation, QCoreApplication, QEvent, Qt
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


def two_frame_gif(loop=None, delay_cs=12, colours=(1, 2)):
    """Hand-built 4x4 GIF: transparent border, red then green centre."""
    result = b'GIF89a'+struct.pack('<HHBBB', 4, 4, 0x81, 0, 0)
    result += bytes((0, 0, 0, 255, 0, 0, 0, 255, 0, 0, 0, 255))
    if loop is not None:
        result += b'\x21\xff\x0bNETSCAPE2.0\x03\x01'+struct.pack('<H', loop)+b'\x00'
    for colour in colours:
        result += b'\x21\xf9\x04\x09'+struct.pack('<H', delay_cs)+b'\x00\x00'
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

    def test_gif_plays_once_and_disappears_after_last_frame_delay_even_if_looping(self):
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
                pump(160)
                self.assertEqual(movie.state(), QMovie.NotRunning)
                self.assertEqual(movie.currentFrameNumber(), 1)
                self.assertNotIn(0, frames, 'the first frame must not repeat after initial playback')
                image = movie.currentPixmap().toImage()
                self.assertEqual(image.pixelColor(image.width()//2, image.height()//2).green(), 255)
                self.assertFalse(self.overlay.kill_icon_label.isHidden())
                pump(180)
                self.assertTrue(self.overlay.kill_icon_label.isHidden())
                self.assertIsNone(self.overlay.kill_icon_label.movie())
                self.assertFalse(self.overlay.kill_icon_timer.isActive())
                self.assertFalse(isValid(movie))

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
        pump(90)
        self.assertFalse(self.overlay.kill_icon_label.isHidden(), 'the replaced GIF must not end the new one')
        pump(180)
        self.assertTrue(self.overlay.kill_icon_label.isHidden())

    def test_single_frame_gif_keeps_its_frame_delay_then_disappears(self):
        path = Path(self.temp.name)/'single.gif'
        path.write_bytes(two_frame_gif(loop=0, delay_cs=20, colours=(1,)))
        self.overlay.show_kill_icon(str(path))
        movie = self.movie()
        pump(80)
        self.assertFalse(self.overlay.kill_icon_label.isHidden())
        pump(200)
        self.assertTrue(self.overlay.kill_icon_label.isHidden())
        self.assertFalse(isValid(movie))

    def test_gif_maximum_ends_long_animation_without_static_image_fade(self):
        path = Path(self.temp.name)/'long.gif'
        path.write_bytes(two_frame_gif(loop=0, delay_cs=100))
        self.overlay.kill_icon_gif_max_ms = 200
        self.overlay.show_kill_icon(str(path))
        movie = self.movie()
        pump(90)
        self.assertFalse(self.overlay.kill_icon_label.isHidden())
        pump(170)
        self.assertTrue(self.overlay.kill_icon_label.isHidden())
        self.assertFalse(isValid(movie))
        self.assertEqual(self.overlay.kill_icon_fade_out.state(), QAbstractAnimation.Stopped)

    def test_custom_duration_above_three_seconds_allows_longer_gif(self):
        path = Path(self.temp.name)/'extended.gif'
        path.write_bytes(two_frame_gif(loop=0, delay_cs=300))
        self.overlay.kill_icon_gif_max_ms = 4000
        self.overlay.show_kill_icon(str(path))
        movie = self.movie()
        pump(3200)
        self.assertFalse(self.overlay.kill_icon_label.isHidden())
        pump(950)
        self.assertTrue(self.overlay.kill_icon_label.isHidden())
        self.assertFalse(isValid(movie))

    def test_static_icon_keeps_original_three_second_display(self):
        self.overlay.kill_icon_gif_max_ms = 100
        self.overlay.show_kill_icon(str(self.png))
        pump(200)
        self.assertFalse(self.overlay.kill_icon_label.isHidden())
        self.assertGreater(self.overlay.kill_icon_timer.remainingTime(), 2600)

    def test_switch_to_static_cancels_old_gif_completion(self):
        self.overlay.show_kill_icon(str(self.gif))
        pump(160)
        self.overlay.show_kill_icon(str(self.png))
        pump(200)
        self.assertFalse(self.overlay.kill_icon_label.isHidden())
        self.assertEqual(self.overlay.kill_icon_label.pixmap().toImage().pixelColor(0, 0), Qt.blue)

    def test_switch_to_static_image_releases_gif_and_shows_new_artwork(self):
        self.overlay.show_kill_icon(str(self.gif))
        old = self.movie()
        self.overlay.show_kill_icon(str(self.png)); pump(25)
        self.assertFalse(isValid(old))
        self.assertIsNone(self.overlay.kill_icon_label.movie())
        self.assertEqual(self.overlay.kill_icon_label.pixmap().toImage().pixelColor(0, 0), Qt.blue)

    def test_natural_end_cancels_display_and_releases_decoder(self):
        self.overlay.show_kill_icon(str(self.gif))
        old = self.movie()
        pump(380)
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


class KillIconSizeDefaultsTests(unittest.TestCase):
    setUp = KillIconGifTests.setUp
    close_overlay = KillIconGifTests.close_overlay

    def test_gif_duration_control_persists_and_reaches_game_overlay(self):
        config = Config()
        dialog = self.dialog(config)
        self.assertEqual(dialog.gif_duration_spin.value(), 3.0)
        dialog.gif_duration_spin.setValue(.4)
        dialog.close()
        self.assertEqual(config.get('visual')['kill_icon_gif_max_seconds'], .4)
        reopened = self.dialog(config)
        self.assertEqual(reopened.gif_duration_spin.value(), .4)
        handler = VisualHandler(config)
        try:
            self.assertEqual(handler.overlay.kill_icon_gif_max_ms, 400)
        finally:
            handler.cleanup(); handler.deleteLater(); pump(20)

    def test_runtime_duration_rejects_invalid_values_and_bounds_imported_settings(self):
        config = Config()
        handler = VisualHandler(config)
        try:
            self.assertEqual(handler.overlay.kill_icon_gif_max_ms, 3000)
            for value, milliseconds in ((None, 3000), ('bad', 3000), (True, 3000),
                                         (float('nan'), 3000), (float('inf'), 3000),
                                         (-1, 100), (0, 100), (.25, 250), (99, 30000)):
                with self.subTest(value=value):
                    config.set('visual', {'kill_icon_gif_max_seconds': value})
                    handler._update_config(force=True)
                    self.assertEqual(handler.overlay.kill_icon_gif_max_ms, milliseconds)
        finally:
            handler.cleanup(); handler.deleteLater(); pump(20)

    def dialog(self, config):
        parent = QWidget(); parent.resize(900, 900)
        dialog = KillIconDialog(config, parent)
        self.addCleanup(lambda: (dialog.close(), parent.close(), dialog.deleteLater(), parent.deleteLater(), pump(20)))
        return dialog

    def select(self, dialog, path, index=None):
        with patch('app.ui.pages.visual_page.QFileDialog.getOpenFileName', return_value=(str(path), '')):
            if index is None: dialog._browse_basic_image()
            else: dialog._browse_adv_image(index)

    def test_first_global_gif_selection_sets_320_in_saved_settings_and_controls(self):
        config = Config({'kill_icon_width': 120, 'kill_icon_height': 120})
        dialog = self.dialog(config); self.select(dialog, self.gif)
        self.assertEqual((dialog.width_slider.value(), dialog.height_slider.value()), (320, 320))
        self.assertEqual((config.get('visual')['kill_icon_width'], config.get('visual')['kill_icon_height']), (320, 320))

    def test_first_advanced_slot_gif_selection_sets_320(self):
        config = Config({'kill_icon_is_advanced': True, 'kill_icon_width': 120, 'kill_icon_height': 120})
        dialog = self.dialog(config); self.select(dialog, self.gif, 4)
        self.assertEqual((dialog.width_slider.value(), dialog.height_slider.value()), (320, 320))
        self.assertEqual(config.get('visual')['kill_icons_1_5'][4]['path'], str(self.gif))

    def test_enabling_existing_gif_slots_synchronizes_size_controls_before_runtime_tick(self):
        config = Config({'kill_icon_is_advanced': False, 'kill_icon_path': str(self.png),
                         'kill_icons_1_5': [{'path': str(self.gif)}],
                         'kill_icon_width': 120, 'kill_icon_height': 120})
        dialog = self.dialog(config)
        self.assertEqual((dialog.width_slider.value(), dialog.height_slider.value()), (120, 120))
        dialog.adv_switch.setChecked(True)
        self.assertEqual((dialog.width_slider.value(), dialog.height_slider.value()), (320, 320))
        self.assertEqual((config.get('visual')['kill_icon_width'], config.get('visual')['kill_icon_height']), (320, 320))

    def test_existing_custom_dimensions_and_bottom_offset_survive_gif_selection(self):
        config = Config({'kill_icon_width': 321, 'kill_icon_height': 316, 'kill_icon_bottom': 0})
        dialog = self.dialog(config); self.select(dialog, self.gif)
        visual = config.get('visual')
        self.assertEqual((visual['kill_icon_width'], visual['kill_icon_height'], visual['kill_icon_bottom']), (321, 316, 0))
        self.assertEqual((dialog.width_slider.value(), dialog.height_slider.value()), (321, 316))

    def test_manual_choice_of_120_is_retained_for_future_gifs_and_reopen(self):
        config = Config({'kill_icon_width': 120, 'kill_icon_height': 120})
        dialog = self.dialog(config)
        dialog.width_slider.setValue(150); dialog.width_slider.setValue(120)
        self.select(dialog, self.gif)
        self.assertEqual((dialog.width_slider.value(), dialog.height_slider.value()), (120, 120))
        self.assertTrue(config.get('visual').get('kill_icon_size_customized'))
        reopened = self.dialog(config)
        self.assertEqual((reopened.width_slider.value(), reopened.height_slider.value()), (120, 120))

    def test_runtime_migrates_untouched_gif_default_before_opening_settings(self):
        config = Config({'flash_enabled': False, 'kill_icon_enabled': True, 'kill_icon_path': str(self.gif),
                         'kill_icon_width': 120, 'kill_icon_height': 120})
        handler = VisualHandler(config)
        try:
            self.assertEqual((handler.overlay.kill_icon_width, handler.overlay.kill_icon_height), (320, 320))
            self.assertEqual((config.get('visual')['kill_icon_width'], config.get('visual')['kill_icon_height']), (320, 320))
        finally:
            handler.cleanup(); handler.deleteLater(); pump(20)

    def test_static_active_icon_and_inactive_gif_slots_keep_original_dimensions(self):
        config = Config({'flash_enabled': False, 'kill_icon_path': str(self.png), 'kill_icon_width': 120,
                         'kill_icon_height': 120, 'kill_icon_is_advanced': False,
                         'kill_icons_1_5': [{'path': str(self.gif)}]})
        handler = VisualHandler(config)
        try:
            self.assertEqual((handler.overlay.kill_icon_width, handler.overlay.kill_icon_height), (120, 120))
        finally:
            handler.cleanup(); handler.deleteLater(); pump(20)


class KillIconPreviewTests(unittest.TestCase):
    def test_preview_draws_inside_dialog_and_fades_in_place(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'visible.png'
            image = QImage(4, 4, QImage.Format_ARGB32); image.fill(Qt.red); image.save(str(path))
            parent = QWidget(); parent.resize(900, 900); parent.show()
            dialog = KillIconDialog(Config({'kill_icon_path': str(path)}), parent)
            try:
                dialog.show(); dialog._preview_icon(); pump(250)
                centre = dialog.preview_label.mapTo(dialog, dialog.preview_label.rect().center())
                colour = dialog.grab().toImage().pixelColor(centre)
                self.assertLess(colour.blue(), 64, 'the artwork must be drawn inside its preview rectangle')
                pump(3050)
                colour = dialog.grab().toImage().pixelColor(centre)
                self.assertGreater(colour.blue(), 64)
                self.assertLess(colour.blue(), 255)
            finally:
                dialog.close(); parent.close(); dialog.deleteLater(); parent.deleteLater(); pump(30)

    def test_preview_gif_natural_end_and_configured_limit_match_overlay_behavior(self):
        with tempfile.TemporaryDirectory() as folder:
            short = Path(folder)/'short.gif'; short.write_bytes(two_frame_gif(loop=0))
            long = Path(folder)/'long.gif'; long.write_bytes(two_frame_gif(loop=0, delay_cs=100))
            parent = QWidget(); parent.resize(900, 900); parent.show()
            config = Config({'kill_icon_path': str(short), 'kill_icon_is_advanced': True,
                             'kill_icons_1_5': [{'path': str(long)}]})
            dialog = KillIconDialog(config, parent)
            try:
                dialog.show(); dialog._preview_icon(); pump(80)
                centre = dialog.preview_label.mapTo(dialog, dialog.preview_label.rect().center())
                self.assertLess(dialog.grab().toImage().pixelColor(centre).blue(), 64)
                pump(260)
                self.assertTrue(dialog.preview_label.isHidden())
                self.assertIsNone(dialog.preview_label.movie())
                self.assertFalse(dialog.preview_timer.isActive())
                dialog.gif_duration_spin.setValue(.4)
                dialog._preview_icon(0); pump(200)
                self.assertFalse(dialog.preview_label.isHidden())
                pump(280)
                self.assertTrue(dialog.preview_label.isHidden())
                self.assertIsNone(dialog.preview_label.movie())
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
