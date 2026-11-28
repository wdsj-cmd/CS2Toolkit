"""Small, locally painted runner; no browser, media decoder or asset downloads."""
from dataclasses import dataclass
import math
import random
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QPen, QPolygonF, QShortcut
from PySide6.QtWidgets import QPushButton, QWidget


@dataclass
class Cactus:
    x: float
    width: float
    height: float


class DinoRunner:
    def __init__(self):
        self.height = 0.0
        self.velocity = 0.0
        self.distance = 0.0
        self.crashed = False
        self.obstacles = []
        self._next_obstacle = 1.5

    def jump(self):
        if self.height == 0 and not self.crashed:
            self.velocity = 650.0

    def advance(self, seconds):
        if self.crashed:
            return
        dt = min(max(seconds, 0), .05)
        speed = min(420, 260 + self.distance / 120)
        self.distance += speed * dt
        if self.height > 0 or self.velocity > 0:
            self.height = max(0.0, self.height + self.velocity * dt - 900 * dt * dt)
            self.velocity -= 1800 * dt
            if self.height == 0:
                self.velocity = 0.0
        self._next_obstacle -= dt
        if self._next_obstacle <= 0:
            self.obstacles.append(Cactus(740, random.choice((19, 25, 32)), random.choice((36, 44, 52))))
            self._next_obstacle = random.uniform(1.4, 2.2)
        dino = QRectF(89, 257 - self.height, 29, 40)
        for obstacle in self.obstacles:
            obstacle.x -= speed * dt
            if dino.intersects(QRectF(obstacle.x, 300-obstacle.height, obstacle.width, obstacle.height)):
                self.crashed = True
        self.obstacles = [obstacle for obstacle in self.obstacles if obstacle.x + obstacle.width > 0]


class OfflineDinoWindow(QWidget):
    proceed = Signal()
    dismissed = Signal()
    DINO = (
        '       XXXXXXXX', '       XXXXXX X', '       XXXXXXXX', '       XXXXX   ',
        ' X    XXXXXXXX ', ' XX  XXXXX     ', ' XXXXXXXXXX    ', ' XXXXXXXXX     ',
        '  XXXXXXXX     ', '   XXXXXX      ', '    XX XX      ', '    X   X      ',
    )

    def __init__(self, preview=False):
        super().__init__()
        self.setWindowTitle('CS2 工具箱 · 离线恐龙小游戏' + ('（调试）' if preview else ''))
        self.resize(720, 420)
        self.setMinimumSize(580, 350)
        self.setFocusPolicy(Qt.StrongFocus)
        self.runner = DinoRunner()
        self.preview = preview
        self.reconnecting = False
        self._meteor_elapsed = 0.0
        self._finished = False
        self._closed = False
        self._last_tick = time.monotonic()
        self.frame_timer = QTimer(self)
        self.frame_timer.setInterval(34)  # At most 30 FPS; active only while this window is visible.
        self.frame_timer.timeout.connect(self._tick)
        self.skip_button = QPushButton('跳过游戏，进入工具箱（Esc）', self)
        self.skip_button.clicked.connect(self._skip)
        self.simulate_button = QPushButton('模拟恢复联网', self)
        self.simulate_button.setVisible(preview)
        self.simulate_button.clicked.connect(self.connection_restored)
        self._shortcuts = []
        for key, callback in ((Qt.Key_Escape, self._skip), (Qt.Key_Space, self._jump), (Qt.Key_Up, self._jump)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.WindowShortcut)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)
        self.setStyleSheet('QPushButton { padding: 7px 12px; border: 1px solid #bac5cd; '
                           'border-radius: 5px; background: white; color: #273846; } '
                           'QPushButton:hover { background: #e6f1f7; }')

    def resizeEvent(self, event):
        self.skip_button.setGeometry(self.width()-264, self.height()-48, 246, 32)
        self.simulate_button.setGeometry(18, self.height()-48, 144, 32)
        super().resizeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._finished and not self._closed and (self.reconnecting or not self.runner.crashed):
            self._last_tick = time.monotonic()
            self.frame_timer.start()

    def hideEvent(self, event):
        self.frame_timer.stop()
        super().hideEvent(event)

    def changeEvent(self, event):
        if self.isMinimized():
            self.frame_timer.stop()
        elif (self.isVisible() and not self._finished and not self._closed
              and (self.reconnecting or not self.runner.crashed)):
            self._last_tick = time.monotonic()
            self.frame_timer.start()
        super().changeEvent(event)

    def closeEvent(self, event):
        self.frame_timer.stop()
        if not self._closed:
            self._closed = True
            self.dismissed.emit()
        event.accept()

    def shutdown(self):
        self._closed = True
        self.frame_timer.stop()
        self.hide()
        self.close()

    def _skip(self):
        if not self._finished and not self._closed:
            self._finished = True
            self.frame_timer.stop()
            self.proceed.emit()

    def _jump(self):
        if self.reconnecting or self._finished:
            return
        if self.runner.crashed:
            self.runner = DinoRunner()
            self._last_tick = time.monotonic()
            if self.isVisible() and not self.isMinimized():
                self.frame_timer.start()
        self.runner.jump()
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._jump()
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        # Also accept direct key events before the native window has acquired focus.
        if event.key() == Qt.Key_Escape:
            self._skip()
        elif event.key() in (Qt.Key_Space, Qt.Key_Up):
            self._jump()
        else:
            super().keyPressEvent(event)

    def connection_restored(self):
        if self.reconnecting or self._finished or self._closed:
            return
        self.reconnecting = True
        self._meteor_elapsed = 0.0
        self.simulate_button.setEnabled(False)
        # Reconnection should finish even if the window was minimized.
        self.showNormal()
        self._last_tick = time.monotonic()
        self.frame_timer.start()
        self.update()

    def _tick(self):
        now = time.monotonic()
        elapsed = min(now-self._last_tick, .05)
        self._last_tick = now
        if self.reconnecting:
            self._meteor_elapsed += elapsed
            if self._meteor_elapsed >= 1.1:
                self._skip()
                return
        else:
            self.runner.advance(elapsed)
            if self.runner.crashed:
                self.frame_timer.stop()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#f7fafc'))
        scale = min(self.width()/720, (self.height()-58)/350)
        painter.translate((self.width()-720*scale)/2, 8)
        painter.scale(scale, scale)
        painter.setRenderHint(QPainter.Antialiasing, False)
        ink = QColor('#354655')
        painter.setPen(ink)
        painter.setFont(QFont('Microsoft YaHei', 17))
        title = '网络已恢复，准备进入工具箱…' if self.reconnecting else '网络暂未连接，跑一会儿吧'
        if self.preview and not self.reconnecting:
            title = '离线小游戏调试 · 无需断网'
        painter.drawText(QRectF(0, 12, 720, 38), Qt.AlignCenter, title)
        painter.setFont(QFont('Microsoft YaHei', 10))
        painter.drawText(QRectF(0, 52, 720, 25), Qt.AlignCenter, '空格 / ↑ / 点击跳跃，按 Esc 随时跳过小游戏，直接进入工具箱')
        painter.setFont(QFont('Consolas', 13))
        painter.drawText(QRectF(510, 91, 170, 24), Qt.AlignRight, f'{int(self.runner.distance/12):05d}')
        painter.setPen(QPen(QColor('#a7b4be'), 1))
        painter.drawLine(26, 300, 694, 300)
        for index in range(12):
            x = (index*71-self.runner.distance*.5) % 670 + 25
            painter.drawLine(QPointF(x, 309+index%3*4), QPointF(x+8, 309+index%3*4))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor('#e1e8ee'))
        for x, y in ((200, 140), (425, 172), (625, 130)):
            painter.drawRect(QRectF(x, y, 42, 8))
            painter.drawRect(QRectF(x+9, y-8, 22, 8))
        painter.setBrush(ink)
        for cactus in self.runner.obstacles:
            x, y, width, height = cactus.x, 300-cactus.height, cactus.width, cactus.height
            painter.drawRect(QRectF(x+width*.35, y, width*.3, height))
            painter.drawRect(QRectF(x, y+10, width*.22, height*.45))
            painter.drawRect(QRectF(x, y+height*.5, width*.4, 5))
            painter.drawRect(QRectF(x+width*.78, y+5, width*.22, height*.4))
            painter.drawRect(QRectF(x+width*.55, y+height*.42, width*.45, 5))
        impact = self.reconnecting and self._meteor_elapsed >= .72
        if not impact:
            pixel = 4
            for row, line in enumerate(self.DINO):
                for column, char in enumerate(line):
                    if char == 'X':
                        painter.drawRect(QRectF(70+column*pixel, 252-self.runner.height+row*pixel, pixel, pixel))
            if self.runner.height == 0 and not self.runner.crashed and not self.reconnecting:
                foot = int(self.runner.distance/32) % 2
                painter.drawRect(QRectF(86+foot*16, 296, 12, 4))
        if self.runner.crashed and not self.reconnecting:
            painter.setPen(ink)
            painter.setFont(QFont('Microsoft YaHei', 14))
            painter.drawText(QRectF(0, 195, 720, 32), Qt.AlignCenter, '撞到仙人掌了！按空格再来一次')
        if self.reconnecting:
            self._paint_meteor(painter)

    def _paint_meteor(self, painter):
        progress = min(self._meteor_elapsed/.72, 1)
        x, y = 585-482*progress, 85+190*progress
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        if progress < 1:
            painter.setBrush(QColor('#ffbc62'))
            painter.drawPolygon(QPolygonF([QPointF(x-8, y+8), QPointF(x+85, y-60), QPointF(x+12, y+20)]))
            painter.setBrush(QColor('#677d90'))
            painter.drawEllipse(QPointF(x, y), 26, 26)
            painter.setPen(QPen(QColor('white'), 3, Qt.SolidLine, Qt.RoundCap))
            for radius in (9, 17):
                painter.drawArc(QRectF(x-radius, y-radius+6, 2*radius, 2*radius), 45*16, 90*16)
            painter.setBrush(QColor('white'))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QPointF(x, y+9), 2.6, 2.6)
            painter.setPen(QColor('#354655'))
            painter.setFont(QFont('Consolas', 11, QFont.Bold))
            painter.drawText(QRectF(x-30, y-47, 60, 20), Qt.AlignCenter, 'Wi-Fi')
        else:
            age = (self._meteor_elapsed-.72)/.38
            for index in range(9):
                angle = index*math.tau/9
                radius = 16+age*65
                painter.setBrush(QColor('#ffab49' if index%2 else '#8396a5'))
                painter.drawRect(QRectF(103+math.cos(angle)*radius, 278+math.sin(angle)*radius*.5, 7, 7))
