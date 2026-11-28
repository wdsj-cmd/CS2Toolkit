"""Static and animated kill artwork with a bounded decoder lifetime."""
from PySide6.QtCore import Qt, Signal, Property, QTimer
from PySide6.QtGui import QMovie, QPixmap, QPainter
from PySide6.QtWidgets import QLabel


class KillIconLabel(QLabel):
    failed = Signal()
    finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._movie = None
        self._media_opacity = 1.0
        self._gif_end_timer = QTimer(self)
        self._gif_limit_timer = QTimer(self)
        for timer in (self._gif_end_timer, self._gif_limit_timer):
            timer.setSingleShot(True)
            timer.setTimerType(Qt.PreciseTimer)
            timer.timeout.connect(self._finish_gif)
        self.setScaledContents(True)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet('background: transparent;')

    def set_media(self, path, gif_max_ms=3000):
        self.clear_media()
        if not path:
            return False
        if str(path).lower().endswith('.gif'):
            movie = QMovie(str(path), parent=self)
            # Decode on demand: large GIFs must not cache every frame in RAM.
            movie.setCacheMode(QMovie.CacheNone)
            movie.setScaledSize(self.size())
            if not movie.isValid():
                movie.deleteLater()
                return False
            self._movie = movie
            self.setMovie(movie)
            movie.frameChanged.connect(self._stop_at_last_frame)
            movie.finished.connect(self._movie_finished)
            movie.error.connect(self._decode_failed)
            self._gif_limit_timer.start(gif_max_ms)
            movie.start()
            if self._movie is not movie or movie.currentPixmap().isNull():
                self.clear_media()
                return False
        else:
            pixmap = QPixmap(str(path))
            if pixmap.isNull():
                return False
            self.setPixmap(pixmap)
        return True

    def _stop_at_last_frame(self, frame):
        movie = self.sender()
        if movie is self._movie:
            count = movie.frameCount()
            if count > 0 and frame == count-1:
                # Show the last frame for its encoded delay, then end the
                # animation instead of allowing embedded looping or holding it.
                delay = movie.nextFrameDelay()
                movie.stop()
                self._gif_end_timer.start(max(0, delay))

    def _movie_finished(self):
        if self.sender() is self._movie:
            self._finish_gif()

    def _finish_gif(self):
        if self._movie is not None:
            self.hide()
            self.clear_media()
            self.finished.emit()

    def clear_media(self):
        self._gif_end_timer.stop()
        self._gif_limit_timer.stop()
        movie, self._movie = self._movie, None
        self.setMovie(None)
        self.clear()
        if movie is not None:
            movie.stop()
            movie.deleteLater()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._movie is not None:
            self._movie.setScaledSize(event.size())

    def _get_media_opacity(self):
        return self._media_opacity

    def _set_media_opacity(self, value):
        self._media_opacity = max(0.0, min(1.0, value))
        self.update()

    mediaOpacity = Property(float, _get_media_opacity, _set_media_opacity)

    def paintEvent(self, event):
        if self._media_opacity >= 1:
            super().paintEvent(event)
            return
        # Nested QGraphicsOpacityEffects (the settings dialog already has
        # one) can paint a movie at the wrong offset. Fade its pixels here.
        pixmap = self._movie.currentPixmap() if self._movie is not None else self.pixmap()
        if not pixmap.isNull():
            painter = QPainter(self)
            painter.setOpacity(self._media_opacity)
            painter.drawPixmap(self.rect(), pixmap)

    def _decode_failed(self, _error):
        if self.sender() is self._movie:
            self.hide()
            self.clear_media()
            self.failed.emit()

    def hideEvent(self, event):
        self.clear_media()
        super().hideEvent(event)
