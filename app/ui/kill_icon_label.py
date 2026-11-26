"""Static and animated kill artwork with a bounded decoder lifetime."""
from PySide6.QtCore import Qt, Signal, Property
from PySide6.QtGui import QMovie, QPixmap, QPainter
from PySide6.QtWidgets import QLabel


class KillIconLabel(QLabel):
    failed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._movie = None
        self._media_opacity = 1.0
        self.setScaledContents(True)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet('background: transparent;')

    def set_media(self, path):
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
            movie.error.connect(self._decode_failed)
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
                # Ignore embedded loop metadata. Keep the last frame visible
                # until the existing display timer fades the artwork out.
                movie.stop()

    def clear_media(self):
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
