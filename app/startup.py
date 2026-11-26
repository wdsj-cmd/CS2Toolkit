"""A short, asynchronous connectivity check before the normal toolkit startup."""
from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest


class ConnectivityProbe(QObject):
    status_changed = Signal(bool)

    def __init__(self, urls=None, *, timeout_ms=1200, interval_ms=3000, parent=None):
        super().__init__(parent)
        self.urls = tuple(urls or ('https://www.baidu.com/favicon.ico',
                                  'https://www.microsoft.com/favicon.ico'))
        self.timeout_ms = timeout_ms
        self.manager = QNetworkAccessManager(self)
        self.retry_timer = QTimer(self)
        self.retry_timer.setInterval(interval_ms)
        self.retry_timer.setSingleShot(True)
        self.retry_timer.timeout.connect(self._check)
        self._replies = set()
        self._generation = 0
        self._last_status = None
        self.is_running = False

    def start(self):
        if self.is_running:
            return
        self.is_running = True
        self._last_status = None
        self._check()

    def stop(self):
        self.is_running = False
        self._generation += 1
        self.retry_timer.stop()
        replies, self._replies = self._replies, set()
        for reply in replies:
            reply.abort()
            reply.deleteLater()

    def _check(self):
        if not self.is_running or self._replies:
            return
        self._generation += 1
        generation = self._generation
        for url in self.urls:
            request = QNetworkRequest(QUrl(url))
            request.setTransferTimeout(self.timeout_ms)
            request.setAttribute(QNetworkRequest.CacheLoadControlAttribute,
                                 QNetworkRequest.AlwaysNetwork)
            reply = self.manager.head(request)
            self._replies.add(reply)
            reply.finished.connect(lambda current=reply: self._finished(current, generation))

    def _finished(self, reply, generation):
        if generation != self._generation or not self.is_running:
            return
        self._replies.discard(reply)
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        online = (reply.error() == QNetworkReply.NoError
                  and status is not None and 200 <= int(status) < 400)
        reply.deleteLater()
        if online:
            # Invalidate callbacks before aborting other requests: abort may emit synchronously.
            self._generation += 1
            pending, self._replies = self._replies, set()
            for other in pending:
                other.abort()
                other.deleteLater()
            self._publish(True)
        elif not self._replies:
            self._publish(False)

    def _publish(self, online):
        if online != self._last_status:
            self._last_status = online
            self.status_changed.emit(online)
        if self.is_running:
            self.retry_timer.start()


def _create_main_window():
    # Keep the offline game independent of the toolkit's audio, capture and web services.
    from app.main_window import CS2Tool
    return CS2Tool()


class StartupController(QObject):
    def __init__(self, main_window_factory=None, *, probe=None, parent=None):
        super().__init__(parent)
        self.main_window_factory = main_window_factory or _create_main_window
        self.probe = probe if probe is not None else ConnectivityProbe(parent=self)
        self.probe.status_changed.connect(self._network_changed)
        self.game = None
        self.main_window = None
        self._opening = False
        self._stopped = False
        self._preview = False

    def start(self, preview=False):
        self._preview = preview
        if preview:
            self._show_game()
        else:
            self.probe.start()

    def stop(self):
        self._stopped = True
        self.probe.stop()
        if self.game is not None:
            self.game.shutdown()

    def _show_game(self):
        if self.game is not None or self._stopped:
            return
        from app.ui.offline_dino import OfflineDinoWindow
        self.game = OfflineDinoWindow(preview=self._preview)
        self.game.proceed.connect(self._open_main)
        self.game.dismissed.connect(self._dismissed)
        if self._preview:
            # Preview uses the existing window, never a second toolkit instance.
            self.main_window = self.main_window_factory()
            self.main_window.hide()
        self.game.show()
        self.game.activateWindow()
        self.game.setFocus()

    def _network_changed(self, online):
        if self._stopped or self._opening:
            return
        if online:
            if self.game is None:
                self._open_main()
            else:
                self.probe.stop()
                self.game.connection_restored()
        else:
            self._show_game()

    def _open_main(self):
        if self._opening or self._stopped:
            return
        self._opening = True
        self.probe.stop()
        if self.main_window is None:
            self.main_window = self.main_window_factory()
        self.main_window.show()
        self.main_window.activateWindow()
        self.main_window.raise_()
        if self.game is not None:
            self.game.shutdown()

    def _dismissed(self):
        if self._preview:
            self._open_main()
        else:
            from PySide6.QtWidgets import QApplication
            self.stop()
            QApplication.instance().quit()

    def focus(self):
        window = self.main_window if self._opening else self.game
        if window is not None:
            if hasattr(window, 'show_window'):
                window.show_window()
                return
            window.showNormal() if window.isMinimized() else window.show()
            window.raise_()
            window.activateWindow()
