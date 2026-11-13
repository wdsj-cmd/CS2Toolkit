"""Confirm the team picker after a foreground M key, without intercepting input."""
import base64
import zlib

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

_ICON_FEATURES = (
    (
        'eNoVkutPUmEAh/+avrc+2Nq6l1kh98O4HM57roCHc+DldrjJQUVBnBSKl9IUVFTEDI1DJiqW4n1rZmtlTfuWX1v/'
        'QbQ9H5/tt/32kAAhsf/gJg2KyDDkPqQeB1i5n5WzxBNU02zWKwGqwTEtgSENKMJIASNu0uK6R8mQrjhmK2ft5Rwv'
        '5biVSXZ+iIlCFYY0A7MOB3oCNzAkSpj1jLllboCpTPHVeVgremYyVmka1hY8a7MuadLxMkkDfTOOGUnCxJAmXN+S'
        'z9CrM46tRe/u28DRu3BfO5pL244r7bul4MaCW5rixhIEqm+lCIAZtamosTL9X16bdWe6wHCMchKtQpu2P0IMduFb'
        'RW9jqDzJBXkVjmOk8cnSeFu1APdWgifrXVtLoSCvYc1yFnsa9+sOJfFTNVov+dfycDpNo3qFz9q6NG6pvfYcr4a/'
        'fuz6dZDsi5hZILMB2ZsJeL4f/7IVO5QiGwX34ghtAYqYTzvRT6Wi6PE78cd2/Fs91e3Vnta6N4qhtIheHPd/3e7c'
        'l4Jhtnm4Q+em5TGfupxzRL06EWrqJfF7vef3Sebv2dCfs6Gfu8mLw96Nea8Vuc6bbqy8skNa5m1TSFl2fcH7LIpx'
        'RGthhLs4en75OX15kjrf738RN6ruXAm3PXw/B0sTDsZwhzTLS2P26ryr/jYwN2rrCaARh/rLh77TzTilvgbBrfFe'
        'sFMSqnmYTVNWw12cBqmIqTzF1xZ9B+VwZc41kqA9FgWpaerxKJdfsfsrwc2Ct5zlw7xSsKlZt48wq/KDttVpvnH1'
        '3nJgQNRi8iYeKCdS1p3lhuyRptjRBKAN92MCxgmiFQoMkM00eshxz0QdpryazzDFcU5glWNJopHTi17cangQhmhK'
        'tDiDnQ5/Bwv9JKZKBJCFUdvsIC3l7JUsXxhh8kNMxKkmdbfdFtVAjHsetcJQtyvUw4dijkAHY+dJgDgtWoFVCHal'
        'i5ETyD2gvumknqa7ueEETHfa/gEdEnv0'
    ),
    (
        'eNpFkttPGkEYxXd23QUWdoEFRK0WsCoIC4LIRUGRW8QLgigUFUTLgsplsZpobMGa2KrVxNq0WuOtxj406V/ZqT70'
        'PM58J+f3nZl0YbvI17laPV/eznL8ZLo44IuaBkZNAz7baCyYWElxW/nKLld9X6g1CnwjubJZfruX39ibzpQ6jTZC'
        'KAEABwgUCgCKEyKt3hZdKHO1RmWzUd7aT+T4tVp9IpGTMAoUxejmVqW6DXkSLWOUrS/hIUkrQvHc2sYev32QyFYm'
        '0wVKrsJQQixTsM5hnCCf5wGKmd0+iVyJYhhJMfFMid85yOR5nWkAAEBSsnaDpdfmBgBj++wGto8Uizt7LVrWRtIy'
        'BABNj6XA118vl3CBCBeQihdaVzjKqFoFQlE8mZmIJyU0TVJSdySu7HhFCEVNhHBuqTQ8NgcQQMqUHUb70MQsiuEi'
        'kTi9sjq3xIklUgjlCE9rzA5KxsD9BwNTRqcfoorlSkcoZvEEIRiG4ancm2yhhDfhACB6u8c1NkMpmuGVye41OQMI'
        'QGhG7YmmWJePUbcFonNjyWwwPh+Kptp0XXrb4EhsUd7SDus12YdHx1MwlBBSxiGfdyptHwnP5Na0xv6ObnY6s+oJ'
        'jbsjs30jEYKkII83GJtfLAiFFIZhynaNZTCUKvAGu4eUKmBdXVZ3Kl+2jYTVLzthpU2EYLlY3eH4btYBIxCAGVir'
        'NxKjVWqA/ntcqULtDk6w/S5YJkCBrtf6cb9xUtpczBQhP3Q4vH5tjwn5L9Cu6/EEx+H3kNAMt1a9/nJ0uc5fLZXS'
        'sQzFtA75Q+oOzfPks4FRtXj8EYlUnVjIX599uv96+MhVH0vbVx+O8/w7vXVIAPeC8U8GiIgLxHqzo7havjw7fPx+'
        '9Ovi9Obb5d3tz+u7+9ubh8/nF6tb9VB83uwMmJ3B4GRqvcLv13dvzo8fLo7+XJ/+/nHyF/Atobo='
    ),
)  # Normalized team emblems only; no player portraits.
_Y, _X = np.mgrid[:16, :16]
_INTERIOR = (_X - 7.5) ** 2 + (_Y - 7.5) ** 2 <= 6.6 ** 2


def team_menu_region(geometry):
    x, y, width, height = geometry
    side = round(height * .074)
    left = round(width / 2 - height * 10 / 27) - side // 2
    right = round(width / 2 + height * 10 / 27) - side // 2
    return x + left, y + round(height * .0736) - side // 2, right - left + side, side


def _signature(rgb):
    gray = rgb.astype(np.float32) @ np.array([.2126, .7152, .0722], dtype=np.float32)
    values = gray[_INTERIOR]
    if float(values.std()) < 5:
        return None
    values = values - values.mean()
    return values / np.linalg.norm(values)


def _reference_signatures():
    return tuple(_signature(np.frombuffer(zlib.decompress(base64.b64decode(data)), dtype=np.uint8).reshape(16, 16, 3))
                 for data in _ICON_FEATURES)


def team_menu_scores(frame, references):
    if frame is None or frame.isNull() or frame.width() < frame.height() * 2:
        return ()
    side = frame.height()
    scores = []
    for x, reference in zip((0, frame.width() - side), references):
        image = frame.copy(x, 0, side, side).scaled(16, 16, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        image = image.convertToFormat(QImage.Format_RGB888)
        rgb = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(16, image.bytesPerLine())[:, :48].reshape(16, 16, 3)
        signature = _signature(rgb)
        if signature is None:
            return ()
        scores.append(float(np.dot(signature, reference)))
    return tuple(scores)


def is_team_menu(frame, references):
    scores = team_menu_scores(frame, references)
    return len(scores) == 2 and min(scores) >= .86


class TeamMenuDetector:
    def __init__(self):
        self.references = _reference_signatures()
        self.visible = False
        self.pending_until = self.next_probe = 0
        self.hits = self.misses = 0

    def request(self, now, initial=False):
        self.pending_until = now + (.2 if initial else .6)
        self.next_probe = now + .04

    def due(self, now):
        return now >= self.next_probe and (self.visible or now < self.pending_until)

    def dismiss(self):
        previous = self.visible
        self.visible = False
        self.pending_until = self.next_probe = 0
        self.hits = self.misses = 0
        return False if previous else None

    def observe(self, frame, now):
        changed = None
        scores = team_menu_scores(frame, self.references)
        if len(scores) == 2 and min(scores) >= .86:
            self.hits += 1
            self.misses = 0
            # Two very strong, independent emblem matches need no extra frame.
            if (self.hits >= 2 or min(scores) >= .96) and not self.visible:
                self.visible = True
                changed = True
            self.next_probe = now + (.4 if self.visible else .06)
        else:
            self.hits = 0
            self.misses += 1
            if self.visible and self.misses >= 2:
                self.visible = False
                changed = False
            self.next_probe = now + .06
        return changed
