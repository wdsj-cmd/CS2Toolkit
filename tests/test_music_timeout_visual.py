import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter

from app.logic.music_round_clock import PauseReadings, PauseBannerTracker, RoundClockMonitor, WindowsClockReader, PauseBannerReader, pause_icon_visible, is_pause_banner, pause_banner_candidate, pause_banner_region, round_clock_region
from app.logic.music_box_events import MusicEventEngine
from app.logic.music_pause_timer import pause_timer_region
from test_music_pause_timer import timer_frame
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


def banner(timer='0:44'):
    image = QImage(450, 54, QImage.Format_RGB32); image.fill(QColor('#807060'))
    painter = QPainter(image)
    painter.fillRect(100, 8, 250, 34, QColor('#303030'))
    font = QFont('Arial'); font.setPixelSize(18); font.setBold(True)
    painter.setFont(font); painter.setPen(Qt.white)
    painter.drawText(image.rect(), Qt.AlignCenter, 'T TIMEOUT '+timer)
    painter.end()
    return image


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

    def test_active_team_banner_survives_actual_small_font_ocr_corruption(self):
        # Real OCR result from the reported small HUD, with no player data.
        for text in ('T 队 暂 停 还 9 闫 4', 'CT队暂停还刂0：', 'T队暂停剩余',
                     'ct 队 暂 停 还', '反恐精英队暂停还剩0:09'):
            self.assertTrue(is_pause_banner(text), text)
        for text in ('T队暂停', 'CT队暂停请求0:30', 'T队暂停还未开始0:44',
                     'T队暂停还未生效0:44', '投票通过：T队暂停还剩0:44', 'CT队准备还剩0:44'):
            self.assertFalse(is_pause_banner(text), text)

    def test_pause_symbol_at_multiple_hud_resolutions(self):
        for size in ((144, 49), (108, 37), (192, 65), (72, 25)):
            self.assertIs(pause_icon_visible(hud(pause=True, size=size)), True)

    def test_reported_1440_by_900_layout_and_smaller_hud_symbols(self):
        self.assertEqual(round_clock_region((0, 0, 1440, 900)), (675, 0, 90, 31))
        self.assertEqual(pause_banner_region((0, 0, 1440, 900)), (495, 621, 450, 54))
        for scale in (1, .85, .75, .65, .55, .5):
            image = QImage(90, 31, QImage.Format_RGB32); image.fill(QColor('#202020'))
            h, w, gap = round(30*900/1440*scale), round(12*900/1440*scale), round(5*900/1440*scale)
            left, top = (90-2*w-gap)//2, (31-h)//2
            painter = QPainter(image)
            for x in (left, left+w+gap): painter.fillRect(x, top, w, h, QColor('#ff2020'))
            painter.end()
            self.assertIs(pause_icon_visible(image), True, scale)

    def test_normal_or_red_numeric_timer_cannot_start_pause(self):
        for text in ('1:55', '0:11', '0:01', '0:10', '0:00', '11:11'):
            self.assertIs(pause_icon_visible(hud(text)), False, text)

    def test_whiteout_blank_or_missing_frame_does_not_end_pause(self):
        for color in (Qt.white, Qt.black):
            image = QImage(144, 49, QImage.Format_RGB32); image.fill(color)
            self.assertIsNone(pause_icon_visible(image))
        self.assertIsNone(pause_icon_visible(None))
        self.assertIsNone(pause_icon_visible(QImage()))

    def test_live_banner_precheck_is_not_itself_a_pause_trigger(self):
        image = QImage(450, 54, QImage.Format_RGB32)
        image.fill(QColor('#807060'))
        self.assertIs(pause_banner_candidate(image), False)
        for color in (Qt.white, Qt.black):
            image.fill(color)
            self.assertIsNone(pause_banner_candidate(image))
        self.assertIs(pause_banner_candidate(hud('1:55')), True)
        self.assertFalse(is_pause_banner('1:55'))

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

    def test_confirmed_banner_has_a_longer_independent_absence_check(self):
        readings = PauseReadings()
        readings.observe(True, 0); readings.observe(True, .25)
        for at in (1, 1.3, 1.6, 1.9):
            self.assertIsNone(readings.observe(False, at, end_delay=1))
        self.assertIs(readings.observe(False, 2.2, end_delay=1), False)


class PauseBannerTrackerTests(unittest.TestCase):
    def test_changing_time_digits_do_not_end_the_confirmed_label(self):
        tracker = PauseBannerTracker()
        self.assertTrue(tracker.remember(banner()))
        self.assertIs(tracker.matches(banner('0:43')), True)
        self.assertIs(tracker.matches(banner('0:09')), True)
        scene = QImage(450, 54, QImage.Format_RGB32); scene.fill(QColor('#807060'))
        self.assertIs(tracker.matches(scene), False)
        scene.fill(Qt.white)
        self.assertIsNone(tracker.matches(scene))
        tracker.clear()
        self.assertIsNone(tracker.matches(banner()))

    def test_tracking_cannot_start_from_unreadable_background(self):
        tracker = PauseBannerTracker()
        image = QImage(450, 54, QImage.Format_RGB32); image.fill(QColor('#807060'))
        self.assertFalse(tracker.remember(image))
        self.assertFalse(tracker.ready)


class PauseBannerReaderTests(unittest.TestCase):
    def make_reader(self):
        with patch.object(WindowsClockReader, '__init__', return_value=None):
            return PauseBannerReader()

    def test_suspected_small_pause_text_retries_with_crisp_scaling(self):
        reader = self.make_reader()
        with patch.object(WindowsClockReader, 'read', side_effect=['T 队 停 还 0 ：', 'T 队 暂 停 还 9 闫 4']) as read:
            text = reader.read(QImage())
            self.assertTrue(is_pause_banner(text))
            self.assertEqual(read.call_count, 2)
            self.assertEqual(read.call_args_list[1].args[1], Qt.FastTransformation)

    def test_already_recognized_banners_and_ordinary_scenery_need_one_pass(self):
        for text in ('T队暂停还剩0:44', 'CT队暂停还', '', '准备阶段', '无关地图画面'):
            with self.subTest(text=text), patch.object(WindowsClockReader, 'read', return_value=text) as read:
                self.assertEqual(self.make_reader().read(QImage()), text)
                self.assertEqual(read.call_count, 1)

    def test_vote_or_not_yet_active_text_cannot_be_retried_into_a_false_positive(self):
        for text in ('投票：T队暂停还剩0:44', '请求CT队暂停还剩0:44', 'T队暂停还未生效0:44'):
            with self.subTest(text=text), patch.object(WindowsClockReader, 'read', side_effect=[text, 'T队暂停还剩0:44']) as read:
                self.assertFalse(is_pause_banner(self.make_reader().read(QImage())))
                self.assertEqual(read.call_count, 1)

    def test_numeric_clock_retry_enlarges_tiny_text_and_restores_normal_reader(self):
        with patch.object(WindowsClockReader, '__init__', return_value=None):
            reader = WindowsClockReader()
        reader.scale = 4
        scales = []
        def recognize(image):
            scales.append(reader.scale)
            return '' if len(scales) == 1 else '1:26'
        with patch.object(reader, 'read', side_effect=recognize):
            self.assertEqual(reader.read_pause_clock(QImage()), '1:26')
        self.assertEqual(scales, [4, 6])
        self.assertEqual(reader.scale, 4)

    def test_valid_numeric_clock_needs_no_retry_and_failed_retry_restores_scale(self):
        with patch.object(WindowsClockReader, '__init__', return_value=None):
            reader = WindowsClockReader()
        reader.scale = 4
        with patch.object(reader, 'read', return_value='1:26') as read:
            self.assertEqual(reader.read_pause_clock(QImage()), '1:26')
            self.assertEqual(read.call_count, 1)
        with patch.object(reader, 'read', side_effect=['', OSError('unavailable')]):
            with self.assertRaises(OSError): reader.read_pause_clock(QImage())
        self.assertEqual(reader.scale, 4)


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

    def test_stage_change_waits_for_timer_motion_before_ending_hud_pause(self):
        self.prepare(); self.visual(True); self.packet(phase='live')
        self.assertFalse(self.controller.engine.round_paused)
        self.assertTrue(self.controller._pause_active)
        self.assertFalse(self.player.fade_requests)
        self.assertTrue(self.controller.clock_monitor.pause_context[4])
        self.visual(False)
        self.assertEqual(self.player.fade_requests, [1200])
        self.player.faded_out.emit()
        self.assertEqual(self.names()[-1], 'round_start')

    def test_live_pause_survives_live_heartbeats_and_fades_when_banner_clears(self):
        self.prepare(); self.packet(phase='live'); self.visual(True)
        self.assertEqual(self.names()[-1], 'timeout')
        for _ in range(4): self.packet(phase='live')
        self.assertTrue(self.controller.engine.round_paused)
        self.assertFalse(self.player.fade_requests)
        self.visual(False)
        self.assertEqual(self.player.fade_requests, [1200])
        self.player.faded_out.emit()
        self.assertEqual(self.names()[-1], 'round_start')

    def test_hidden_banner_and_buy_menu_do_not_restart_pause_music(self):
        self.prepare()
        shown, calls, moving, numeric_calls = [True], [], [False], []
        picture = banner(); scene = QImage(450, 54, QImage.Format_RGB32); scene.fill(QColor('#807060'))
        tracker = SimpleNamespace(find=lambda: 42, client_geometry=lambda hwnd: (0, 0, 1440, 900),
            user32=SimpleNamespace(GetForegroundWindow=lambda: 42))
        timer = pause_timer_region((0, 0, 1440, 900))
        source = SimpleNamespace(grab=lambda region: timer_frame('1:25' if moving[0] else '1:26')
            if region == timer else picture if shown[0] else scene, close=lambda: None)
        def read(image):
            calls.append(True)
            return 'T队暂停还剩0:44' if len(calls) == 1 else 'T 队 停 0：'
        reader = SimpleNamespace(read=read, close=lambda: None)
        def read_clock(image): numeric_calls.append(True); return '1:26'
        clock_reader = SimpleNamespace(read=read_clock, close=lambda: None)
        monitor = RoundClockMonitor(source_factory=lambda: source, tracker_factory=lambda: tracker,
            pause_reader_factory=lambda: reader, reader_factory=lambda: clock_reader)
        monitor.pause_sampled.connect(self.controller._on_pause_visual)
        key = ('de_dust2', 1)
        def pump(seconds):
            end = time.monotonic()+seconds
            while time.monotonic() < end:
                self.packet()
                now = time.monotonic()
                monitor.set_context(key, False, now)
                monitor.set_pause_context(key, True, now, False, self.controller._pause_active)
                base.pump(25)
        with patch('app.logic.music_round_clock.BANNER_RECHECK_SECONDS', .3):
            now = time.monotonic(); monitor.set_context(key, False, now)
            monitor.set_pause_context(key, True, now, False, False); monitor.start()
            try:
                pump(4.8)
                self.assertFalse(calls)
                self.assertEqual(len(numeric_calls), 2)
                self.assertEqual(self.names().count('timeout'), 1)
                self.assertFalse(self.player.fade_requests)
                self.assertTrue(self.controller.engine.round_paused)
                shown[0] = False; pump(1.8)
                self.assertFalse(self.player.fade_requests)
                self.assertEqual(self.names().count('timeout'), 1)
                moving[0] = True; pump(1.3)
                self.assertEqual(self.player.fade_requests, [1200])
                self.assertFalse(self.controller.engine.round_paused)
            finally:
                self.assertTrue(monitor.stop())

    def test_background_gsi_changes_and_stale_data_keep_current_pause_cue(self):
        self.prepare(); self.visual(True)
        cue = self.controller._pause_cue
        self.controller.clock_monitor.game_visibility_changed.emit(True, False)
        self.packet(phase='live')
        self.controller._last_gsi = time.monotonic()-10
        self.controller._tick()
        self.assertIs(self.controller._pause_cue, cue)
        self.assertFalse(self.player.fade_requests)
        self.assertEqual(self.names().count('timeout'), 1)
        self.controller.clock_monitor.game_visibility_changed.emit(True, True)
        self.assertFalse(self.player.fade_requests)
        self.visual(False)
        self.assertEqual(self.player.fade_requests, [1200])

    def test_background_gsi_authoritative_resume_is_applied_on_return(self):
        self.prepare(); self.packet('timeout_ct')
        self.controller.clock_monitor.game_visibility_changed.emit(True, False)
        self.packet('freezetime')
        self.assertFalse(self.player.fade_requests)
        self.controller.clock_monitor.game_visibility_changed.emit(True, True)
        self.assertEqual(self.player.fade_requests, [1200])

    def test_buy_menu_during_pause_retains_watch_and_accepts_timer_resume(self):
        self.prepare(); self.visual(True)
        self.packet(activity='menu')
        self.assertTrue(self.controller.clock_monitor.pause_context[1])
        self.assertEqual(self.names().count('timeout'), 1)
        self.visual(False)
        self.assertEqual(self.player.fade_requests, [1200])

    def test_closed_game_clears_background_pause_without_restarting(self):
        self.prepare(); self.visual(True)
        self.controller.clock_monitor.game_visibility_changed.emit(True, False)
        stops = self.player.stops
        self.controller.clock_monitor.game_visibility_changed.emit(False, False)
        self.assertFalse(self.controller._pause_active)
        self.assertGreater(self.player.stops, stops)
        self.assertEqual(self.names().count('timeout'), 1)

    def test_round_end_releases_visual_pause_without_waiting_for_a_new_clock(self):
        self.prepare(); self.visual(True); self.packet(phase='over')
        self.assertFalse(self.controller._pause_active)
        self.assertEqual(self.player.fade_requests, [1200])

    def test_new_round_cannot_keep_previous_visual_pause(self):
        self.prepare(); self.visual(True)
        self.controller.process_gsi({'map': {'name': 'de_dust2', 'phase': 'live', 'round': 2},
                                     'round': {'phase': 'freezetime'}})
        self.assertFalse(self.controller._pause_active)
        self.assertEqual(self.player.fade_requests, [1200])

    def test_round_end_in_background_keeps_music_until_foreground_return(self):
        self.prepare(); self.visual(True)
        self.controller.clock_monitor.game_visibility_changed.emit(True, False)
        self.packet(phase='over')
        self.assertFalse(self.player.fade_requests)
        self.controller.clock_monitor.game_visibility_changed.emit(True, True)
        self.assertEqual(self.player.fade_requests, [1200])

    def test_estimated_bomb_timer_does_not_expire_while_live_pause_is_active(self):
        now = [100.0]
        engine = MusicEventEngine(clock=lambda: now[0])
        packet = {'provider': {'steamid': 'self'}, 'map': {'name': 'de_dust2', 'round': 1, 'phase': 'live'},
                  'round': {'phase': 'live'}, 'player': {'steamid': 'self', 'team': 'T', 'activity': 'playing',
                                                        'state': {'health': 100}}}
        engine.process(packet)
        packet['round']['bomb'] = 'planted'; engine.process(packet)
        engine.local_plant_time = now[0]
        engine.visual_plant_time = now[0]
        before = engine.bomb_deadline
        self.assertIsNotNone(before)
        self.assertTrue(engine.confirm_pause_visual(True, now[0]))
        now[0] += 60; engine.process(packet)
        self.assertTrue(engine.round_paused)
        self.assertTrue(engine.confirm_pause_visual(False, now[0]))
        self.assertEqual(engine.bomb_deadline, before+60)
        self.assertEqual(engine.bomb_deadline-now[0], 40)
        engine.process(packet)
        self.assertEqual(engine.bomb_deadline, before+60)
        self.assertEqual(engine.local_plant_time, 100)
        self.assertEqual(engine.visual_plant_time, 100)

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

    def test_pause_watch_only_runs_with_timeout_clip_in_playable_stages(self):
        self.prepare(clip=False)
        self.assertFalse(self.controller.clock_monitor.pause_context[1])
        self.assign('timeout', duration=0, loop=True); self.packet()
        self.assertTrue(self.controller.clock_monitor.pause_context[1])
        self.packet(activity='menu')
        self.assertFalse(self.controller.clock_monitor.pause_context[1])
        self.packet(phase='live')
        self.assertTrue(self.controller.clock_monitor.pause_context[1])
        self.assertTrue(self.controller.clock_monitor.pause_context[3])
        self.packet(phase='over')
        self.assertFalse(self.controller.clock_monitor.pause_context[1])

    def test_new_round_rejects_old_visual_confirmation(self):
        self.prepare(); old = ('de_dust2', 1)
        self.controller.engine.round_number = 2
        self.visual(True, key=old)
        self.assertFalse(self.controller.engine.round_paused)

    def test_watch_diagnostics_distinguish_missing_clip_from_ready_and_menu(self):
        events = []
        self.controller.diagnostic.connect(lambda event, details: events.append((event, details)))
        self.prepare(clip=False)
        watches = [r for e, r in events if e == 'music_timeout_watch']
        self.assertIn('has_timeout_clip', watches[-1]['blocked_by'])
        self.assign('timeout', duration=0, loop=True); self.packet()
        watches = [r for e, r in events if e == 'music_timeout_watch']
        self.assertTrue(watches[-1]['ready'])
        self.assertEqual(watches[-1]['timeout_clip_count'], 1)
        count = len(events); self.packet()
        self.assertEqual(len(events), count)
        self.packet(activity='menu')
        watches = [r for e, r in events if e == 'music_timeout_watch']
        self.assertIn('not_menu', watches[-1]['blocked_by'])
        self.assertNotIn(str(self.config.work_dir), str(watches))


class PauseWorkerTests(unittest.TestCase):
    def test_stationary_red_region_requires_two_valid_equal_numeric_reads(self):
        for text, expected in (('10 10', False), ('1:26', True)):
            with self.subTest(text=text):
                reads, detected = [], []
                tracker = SimpleNamespace(find=lambda: 42, client_geometry=lambda hwnd: (0, 0, 1440, 900),
                    user32=SimpleNamespace(GetForegroundWindow=lambda: 42))
                source = SimpleNamespace(grab=lambda region: timer_frame(), close=lambda: None)
                def read(image): reads.append(time.monotonic()); return text
                reader = SimpleNamespace(read=read, close=lambda: None)
                monitor = RoundClockMonitor(source_factory=lambda: source, tracker_factory=lambda: tracker,
                                            reader_factory=lambda: reader)
                monitor.pause_sampled.connect(lambda *args: detected.append(args))
                def refresh():
                    now = time.monotonic()
                    monitor.set_context(('map', 1), False, now)
                    monitor.set_pause_context(('map', 1), True, now)
                refresh(); monitor.start()
                try:
                    end = time.monotonic()+4.8
                    while time.monotonic() < end:
                        refresh(); base.pump(20)
                    self.assertGreaterEqual(len(reads), 2)
                    self.assertLessEqual(len(reads), 3)
                    self.assertEqual(any(row[1] for row in detected), expected)
                    self.assertTrue(all(b-a >= .95 for a, b in zip(reads, reads[1:])))
                finally:
                    self.assertTrue(monitor.stop())

    def test_background_stops_capture_and_return_waits_for_timer_motion(self):
        foreground, text, captures, detected, visibility = [42], [None], [], [], []
        tracker = SimpleNamespace(find=lambda: 42, client_geometry=lambda hwnd: (0, 0, 720, 480),
            user32=SimpleNamespace(GetForegroundWindow=lambda: foreground[0]))
        def grab(region):
            captures.append(region)
            return timer_frame(text[0] or '1:26', (720, 480), bars=text[0] is None)
        source = SimpleNamespace(grab=grab, close=lambda: None)
        monitor = RoundClockMonitor(source_factory=lambda: source, tracker_factory=lambda: tracker)
        monitor.pause_sampled.connect(lambda *args: detected.append(args))
        monitor.game_visibility_changed.connect(lambda *args: visibility.append(args))
        def refresh(milliseconds):
            end = time.monotonic()+milliseconds/1000
            while time.monotonic() < end:
                now = time.monotonic()
                monitor.set_context(('map', 1), False, now, watch=True)
                monitor.set_pause_context(('map', 1), True, now)
                base.pump(20)
        refresh(1); monitor.start()
        try:
            refresh(750)
            self.assertTrue(detected[-1][1])
            foreground[0] = 99; refresh(200)
            count = len(captures); refresh(600)
            self.assertEqual(len(captures), count)
            self.assertTrue(detected[-1][1])
            text[0] = '1:26'; foreground[0] = 42; refresh(650)
            self.assertTrue(detected[-1][1])
            text[0] = '1:25'; refresh(1200)
            self.assertFalse(detected[-1][1])
            self.assertIn((True, False), visibility)
            self.assertEqual(visibility[-1], (True, True))
        finally:
            self.assertTrue(monitor.stop())

    def test_live_round_banner_hint_can_start_but_its_absence_cannot_end_pause(self):
        captures, ocr_calls, detections = [], [], []
        shown, moving = [False], [False]
        scene = QImage(450, 54, QImage.Format_RGB32); scene.fill(QColor('#807060'))
        tracker = SimpleNamespace(find=lambda: 42, client_geometry=lambda hwnd: (0, 0, 1440, 900),
            user32=SimpleNamespace(GetForegroundWindow=lambda: 42))
        def grab(region):
            captures.append(region)
            if region == pause_timer_region((0, 0, 1440, 900)):
                return timer_frame('1:24' if moving[0] else '1:25', red=False)
            return banner() if shown[0] else scene
        def read(image): ocr_calls.append(True); return 'CT队暂停还0：'
        source = SimpleNamespace(grab=grab, close=lambda: None)
        reader = SimpleNamespace(read=read, close=lambda: None)
        monitor = RoundClockMonitor(source_factory=lambda: source, tracker_factory=lambda: tracker,
            pause_reader_factory=lambda: reader)
        monitor.pause_sampled.connect(lambda *args: detections.append(args))
        monitor.set_context(('map', 1), False, time.monotonic())
        monitor.set_pause_context(('map', 1), True, time.monotonic(), True)
        monitor.start()
        try:
            base.pump(650)
            self.assertFalse(ocr_calls)
            self.assertFalse(any(r[1] for r in detections))
            self.assertLessEqual(len(captures), 4)
            shown[0] = True; base.pump(1250)
            self.assertTrue(any(r[1] for r in detections))
            self.assertTrue(ocr_calls)
            shown[0] = False; base.pump(600)
            self.assertTrue(detections[-1][1])
            moving[0] = True
            end = time.monotonic()+2
            while detections[-1][1] and time.monotonic() < end:
                at = time.monotonic()
                monitor.set_context(('map', 1), False, at)
                monitor.set_pause_context(('map', 1), True, at, True)
                base.pump(25)
            self.assertFalse(detections[-1][1])
            self.assertTrue(all(region in (pause_banner_region((0, 0, 1440, 900)),
                pause_timer_region((0, 0, 1440, 900))) for region in captures))
        finally:
            self.assertTrue(monitor.stop())

    def test_diagnostic_reports_are_rate_limited_and_can_report_fatal_failure(self):
        monitor = RoundClockMonitor()
        reports = []
        monitor.pause_status_changed.connect(reports.append)
        monitor._pause_status('waiting_foreground')
        monitor._pause_status('waiting_foreground')
        self.assertEqual(len(reports), 1)
        monitor._pause_status('worker_error', force=True, error_type='OSError')
        self.assertEqual(len(reports), 2)
        self.assertEqual(reports[-1]['error_type'], 'OSError')

    def test_black_capture_and_missing_ocr_are_reported_without_image_or_text_payloads(self):
        samples = []
        tracker = SimpleNamespace(find=lambda: 42, client_geometry=lambda hwnd: (0, 0, 1440, 900),
            user32=SimpleNamespace(GetForegroundWindow=lambda: 42))
        frame = QImage(90, 31, QImage.Format_RGB32); frame.fill(Qt.black)
        source = SimpleNamespace(grab=lambda region: frame, close=lambda: None)
        def broken_reader(): raise OSError('private diagnostic text must not be logged')
        monitor = RoundClockMonitor(source_factory=lambda: source, tracker_factory=lambda: tracker,
            pause_reader_factory=broken_reader)
        monitor.pause_status_changed.connect(samples.append)
        monitor.set_context(('map', 1), False, time.monotonic())
        monitor.set_pause_context(('map', 1), True, time.monotonic())
        monitor.start()
        try:
            base.pump(700)
            self.assertEqual(len(samples), 1)
            report = samples[0]
            self.assertEqual(report['window_size'], [1440, 900])
            self.assertEqual(report['stage'], 'timer_sample')
            self.assertEqual(report['timer_kind'], 'unknown')
            self.assertFalse(report['paused'])
            self.assertIsNone(report['ocr_error'])  # No OCR attempted for a blank capture.
            self.assertNotIn('private diagnostic', str(report))
            self.assertNotIn('text', report)
            self.assertNotIn('image', report)
        finally:
            self.assertTrue(monitor.stop())

    def test_banner_is_independent_of_icon_and_ocr_runs_at_most_once_per_second(self):
        banner_reads, detected, evidence = [], [], []
        tracker = SimpleNamespace(find=lambda: 42, client_geometry=lambda hwnd: (0, 0, 2560, 1440),
            user32=SimpleNamespace(GetForegroundWindow=lambda: 42))
        source = SimpleNamespace(grab=lambda region: timer_frame(red=False)
            if region == pause_timer_region((0, 0, 2560, 1440)) else banner(), close=lambda: None)
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
        source = SimpleNamespace(grab=lambda region: timer_frame(bars=True) if phase[0] else timer_frame(red=False), close=lambda: None)
        def broken_reader(): raise OSError('OCR unavailable')
        monitor = RoundClockMonitor(source_factory=lambda: source, tracker_factory=lambda: tracker,
            pause_reader_factory=broken_reader)
        monitor.pause_sampled.connect(lambda *args: detected.append(args))
        key = ('map', 1)
        monitor.set_context(key, False, time.monotonic())
        monitor.set_pause_context(key, True, time.monotonic())
        monitor.start()
        try:
            base.pump(150); phase[0] = True; base.pump(1150)
            self.assertTrue(any(row[1] for row in detected))
        finally:
            self.assertTrue(monitor.stop())

    def test_pause_detection_uses_existing_capture_worker_without_ocr_and_releases_on_disable(self):
        captures, closed, detected = [], [], []
        tracker = SimpleNamespace(find=lambda: 42, client_geometry=lambda hwnd: (0, 0, 2560, 1440),
            user32=SimpleNamespace(GetForegroundWindow=lambda: 42))
        image = timer_frame(bars=True)
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
        source = SimpleNamespace(grab=lambda region: timer_frame(bars=True), close=lambda: None)
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
