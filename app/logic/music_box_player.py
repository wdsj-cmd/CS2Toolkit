"""One interruptible music channel with clip offsets, loops and short fades."""
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, QTimer, Signal, QPropertyAnimation
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer


class MusicBoxPlayer(QObject):
    finished = Signal()
    failed = Signal(str)
    started = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.players = [QMediaPlayer(self), QMediaPlayer(self)]
        self.outputs = [QAudioOutput(self), QAudioOutput(self)]
        self.fades = [QPropertyAnimation(output, b'volume', self) for output in self.outputs]
        self.active = -1
        self.clip = None
        self.master_volume = 50
        self._token = 0
        self._loaded = False
        self._ended = False
        self._source = QUrl()
        for index, player in enumerate(self.players):
            player.setAudioOutput(self.outputs[index])
            player.mediaStatusChanged.connect(lambda status, i=index: self._media_status(i, status))
            player.positionChanged.connect(lambda position, i=index: self._position(i, position))
            player.errorOccurred.connect(lambda error, message, i=index: self._error(i, message))

    def set_volume(self, value):
        self.master_volume = max(0, min(100, int(value)))
        if self.active >= 0 and self.clip:
            self.fades[self.active].stop()
            self.outputs[self.active].setVolume(self._volume())

    def _volume(self):
        return self.master_volume * float(self.clip.get('volume', 100)) / 10000

    def play(self, path, clip):
        if not Path(path).is_file():
            self.stop()
            self.failed.emit('音频文件已丢失，请重新选择切片')
            return False
        self._token += 1
        token = self._token
        previous = self.active
        current = (previous + 1) % 2
        # A reused channel can still hold the same URL. Clear it first so Qt
        # emits fresh load notifications and reapplies the offset and volume.
        self.active = -1
        self.fades[current].stop()
        self.players[current].stop()
        self.players[current].setSource(QUrl())
        self.active = current
        self.clip = dict(clip)
        self._source = QUrl.fromLocalFile(str(Path(path).resolve()))
        self._loaded = self._ended = False
        self.outputs[current].setVolume(0)
        if previous >= 0:
            fade = self.fades[previous]
            fade.stop()
            fade.setDuration(180)
            fade.setStartValue(self.outputs[previous].volume())
            fade.setEndValue(0)
            fade.start()
            QTimer.singleShot(200, lambda: self.players[previous].stop() if token == self._token else None)
        self.players[current].setSource(self._source)
        self.players[current].play()
        return True

    def _media_status(self, index, status):
        if index != self.active or not self.clip or self.players[index].source() != self._source:
            return
        player = self.players[index]
        if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia) and not self._loaded:
            self._loaded = True
            start = round(self.clip.get('start', 0) * 1000)
            if player.duration() > 0 and start >= player.duration():
                self._error(index, '起始位置超出了音频长度')
                return
            player.setPosition(start)
            fade = self.fades[index]
            fade.setDuration(180)
            fade.setStartValue(0)
            fade.setEndValue(self._volume())
            fade.start()
            self.started.emit()
        elif status == QMediaPlayer.EndOfMedia:
            self._clip_end()

    def _position(self, index, position):
        if index != self.active or not self.clip or not self._loaded or self._ended:
            return
        duration = float(self.clip.get('duration', 0))
        if duration > 0 and position >= round((self.clip.get('start', 0) + duration) * 1000):
            self._clip_end()

    def _clip_end(self):
        if self._ended or self.active < 0 or not self.clip:
            return
        if self.clip.get('loop'):
            self.players[self.active].setPosition(round(self.clip.get('start', 0) * 1000))
            self.players[self.active].play()
        else:
            self._ended = True
            self.stop()
            self.finished.emit()

    def _error(self, index, message):
        if index == self.active:
            self.stop()
            self.failed.emit(message or '音频无法播放，请尝试 WAV 或 MP3 格式')

    def stop(self):
        self._token += 1
        self.active = -1
        self.clip = None
        self._source = QUrl()
        for fade, player in zip(self.fades, self.players):
            fade.stop()
            player.stop()
            player.setSource(QUrl())

    def cleanup(self):
        self.stop()
        for player in self.players:
            player.setSource(QUrl())
