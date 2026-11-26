import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import time
import unittest

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtNetwork import QHostAddress, QTcpServer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from app.startup import ConnectivityProbe, StartupController
from app.ui.offline_dino import Cactus, DinoRunner, OfflineDinoWindow


APP = QApplication.instance() or QApplication([])


def wait_until(condition, timeout=2000):
    end = time.monotonic() + timeout / 1000
    while time.monotonic() < end:
        APP.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        if condition():
            return True
        QTest.qWait(5)
    return bool(condition())


class HttpFixture:
    """A real local HTTP endpoint: no external network or browser needed."""
    def __init__(self, status=200):
        self.status = status
        self.requests = 0
        self.clients = []
        self.server = QTcpServer()
        assert self.server.listen(QHostAddress.LocalHost, 0)
        self.server.newConnection.connect(self.accept)
        self.url = f'http://127.0.0.1:{self.server.serverPort()}/online'

    def accept(self):
        while self.server.hasPendingConnections():
            client = self.server.nextPendingConnection()
            self.clients.append(client)
            client.readyRead.connect(lambda socket=client: self.respond(socket))

    def respond(self, client):
        if b'\r\n\r\n' not in bytes(client.peek(client.bytesAvailable())):
            return
        client.readAll()
        self.requests += 1
        if self.status is not None:
            client.write(f'HTTP/1.1 {self.status} Test\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'.encode())
            client.disconnectFromHost()

    def close(self):
        for client in self.clients:
            client.abort()
        self.server.close()


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.http = HttpFixture()
        self.addCleanup(self.http.close)
        self.windows = []
        self.controller = None
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.controller:
            self.controller.stop()
            if self.controller.game:
                self.controller.game.shutdown()
        for window in self.windows:
            window.close()
        APP.processEvents()

    def start(self, urls=None, timeout=150, interval=50):
        self.probe = ConnectivityProbe(urls or [self.http.url], timeout_ms=timeout, interval_ms=interval)
        def create_main():
            window = QWidget()
            self.windows.append(window)
            return window
        self.controller = StartupController(create_main, probe=self.probe)
        self.controller.start()
        return self.controller

    def test_online_start_opens_main_without_dinosaur_or_continued_probing(self):
        controller = self.start()
        self.assertTrue(wait_until(lambda: len(self.windows) == 1))
        self.assertIsNone(controller.game)
        self.assertTrue(self.windows[0].isVisible())
        count = self.http.requests
        QTest.qWait(180)
        self.assertEqual(self.http.requests, count)

    def test_offline_escape_enters_main_and_stops_all_game_work(self):
        self.http.status = 503
        controller = self.start()
        self.assertTrue(wait_until(lambda: controller.game is not None))
        game = controller.game
        self.assertTrue(game.isVisible())
        self.assertFalse(self.windows)
        self.assertIn('Esc', game.skip_button.text())
        QTest.keyClick(game, Qt.Key_Escape)
        self.assertTrue(wait_until(lambda: len(self.windows) == 1))
        self.assertFalse(game.frame_timer.isActive())
        self.assertFalse(self.probe.is_running)

    def test_reconnection_plays_meteor_before_opening_main_exactly_once(self):
        self.http.status = 503
        controller = self.start()
        self.assertTrue(wait_until(lambda: controller.game is not None))
        self.http.status = 200
        self.assertTrue(wait_until(lambda: controller.game.reconnecting))
        self.assertFalse(self.windows)
        QTest.qWait(300)
        self.assertFalse(self.windows)
        self.assertTrue(wait_until(lambda: len(self.windows) == 1, 2000))
        QTest.qWait(200)
        self.assertEqual(len(self.windows), 1)
        self.assertFalse(self.probe.is_running)

    def test_escape_also_skips_reconnection_animation(self):
        self.http.status = 503
        controller = self.start()
        self.assertTrue(wait_until(lambda: controller.game is not None))
        self.http.status = 200
        self.assertTrue(wait_until(lambda: controller.game.reconnecting))
        QTest.keyClick(controller.game, Qt.Key_Escape)
        self.assertTrue(wait_until(lambda: len(self.windows) == 1, 200))

    def test_unresponsive_endpoint_times_out_without_blocking_skip(self):
        self.http.status = None
        controller = self.start(timeout=80)
        self.assertTrue(wait_until(lambda: controller.game is not None, 1500))
        QTest.mouseClick(controller.game.skip_button, Qt.LeftButton)
        self.assertTrue(wait_until(lambda: len(self.windows) == 1, 200))

    def test_one_reachable_endpoint_is_enough(self):
        broken = HttpFixture(503)
        self.addCleanup(broken.close)
        controller = self.start([broken.url, self.http.url])
        self.assertTrue(wait_until(lambda: len(self.windows) == 1))
        self.assertIsNone(controller.game)

    def test_closing_game_cancels_startup_without_opening_main(self):
        self.http.status = 503
        controller = self.start()
        self.assertTrue(wait_until(lambda: controller.game is not None))
        controller.game.close()
        self.assertFalse(self.probe.is_running)
        self.assertFalse(self.windows)
        count = self.http.requests
        QTest.qWait(180)
        self.assertEqual(self.http.requests, count)

    def test_about_preview_never_probes_network_and_simulated_recovery_returns(self):
        self.probe = ConnectivityProbe([self.http.url], interval_ms=50)
        main = QWidget()
        self.windows.append(main)
        main.show()
        self.controller = StartupController(lambda: main, probe=self.probe)
        self.controller.start(preview=True)
        game = self.controller.game
        self.assertTrue(game.isVisible())
        self.assertFalse(main.isVisible())
        self.assertTrue(game.simulate_button.isVisible())
        QTest.mouseClick(game.simulate_button, Qt.LeftButton)
        self.assertTrue(game.reconnecting)
        self.assertTrue(wait_until(main.isVisible, 2000))
        self.assertEqual(self.http.requests, 0)
        self.assertEqual(len(self.windows), 1)

    def test_closing_debug_preview_returns_to_existing_toolkit(self):
        main = QWidget()
        self.windows.append(main)
        self.controller = StartupController(lambda: main)
        self.controller.start(preview=True)
        self.controller.game.close()
        self.assertTrue(wait_until(main.isVisible))


class DinoTests(unittest.TestCase):
    def test_jump_avoids_cactus_that_collides_with_grounded_dinosaur(self):
        grounded = DinoRunner()
        grounded.obstacles = [Cactus(106, 18, 40)]
        grounded.advance(.03)
        self.assertTrue(grounded.crashed)
        jumping = DinoRunner()
        jumping.jump()
        for _ in range(9):
            jumping.advance(.03)
        self.assertGreater(jumping.height, 70)
        jumping.obstacles = [Cactus(106, 18, 40)]
        jumping.advance(.03)
        self.assertFalse(jumping.crashed)

    def test_cannot_jump_again_in_midair_and_eventually_lands(self):
        runner = DinoRunner()
        runner.jump()
        runner.advance(.05)
        velocity = runner.velocity
        runner.jump()
        self.assertEqual(runner.velocity, velocity)
        for _ in range(30):
            runner.advance(.03)
        self.assertEqual(runner.height, 0)

    def test_space_restarts_after_collision_without_entering_main(self):
        game = OfflineDinoWindow()
        self.addCleanup(game.shutdown)
        game.show()
        game.runner.crashed = True
        QTest.keyClick(game, Qt.Key_Space)
        self.assertFalse(game.runner.crashed)
        self.assertTrue(game.isVisible())

    def test_hidden_game_does_not_keep_render_timer_running(self):
        game = OfflineDinoWindow()
        self.addCleanup(game.shutdown)
        game.show()
        self.assertTrue(game.frame_timer.isActive())
        game.hide()
        self.assertFalse(game.frame_timer.isActive())
        game.show()
        self.assertTrue(game.frame_timer.isActive())

    def test_crash_stops_rendering_until_player_restarts(self):
        game = OfflineDinoWindow()
        self.addCleanup(game.shutdown)
        game.show()
        game.runner.obstacles = [Cactus(106, 18, 40)]
        self.assertTrue(wait_until(lambda: game.runner.crashed))
        self.assertFalse(game.frame_timer.isActive())
        QTest.keyClick(game, Qt.Key_Space)
        self.assertTrue(game.frame_timer.isActive())
        self.assertFalse(game.runner.crashed)

    def test_space_with_skip_button_focused_jumps_instead_of_skipping(self):
        game = OfflineDinoWindow()
        self.addCleanup(game.shutdown)
        requested = []
        game.proceed.connect(lambda: requested.append(True))
        game.show()
        game.activateWindow()
        game.skip_button.setFocus()
        QTest.qWait(30)
        QTest.keyClick(game.skip_button, Qt.Key_Space)
        QTest.qWait(60)
        self.assertGreater(game.runner.height, 0)
        self.assertFalse(requested)


if __name__ == '__main__':
    unittest.main()
