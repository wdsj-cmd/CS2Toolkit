"""Resolution-relative pause marks and timer motion; no game memory or OCR."""
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage


def pause_timer_region(geometry):
    x, y, width, height = geometry
    size = min(width, max(2, round(height*.18)))
    return x+(width-size)//2, y, size, min(height, max(2, round(height*.10)))


@dataclass
class TimerView:
    kind: str = 'unknown'
    red: bool = False
    pattern: object = None
    layout_changed: bool = False
    clock_image: object = None


def _components(mask):
    remaining = set(zip(*np.nonzero(mask)))
    groups = []
    while remaining:
        pending = [remaining.pop()]; points = []
        while pending:
            y, x = pending.pop(); points.append((y, x))
            for p in ((y-1, x), (y+1, x), (y, x-1), (y, x+1)):
                if p in remaining:
                    remaining.remove(p); pending.append(p)
        if len(points) >= 2:
            ys, xs = zip(*points)
            groups.append((min(xs), min(ys), max(xs)+1, max(ys)+1, len(points)))
    return groups


class TimerAnalyzer:
    def __init__(self):
        self.box = None
        self.size = None

    def reset(self):
        self.box = self.size = None

    @staticmethod
    def _bars(mask):
        height, width = mask.shape
        parts = _components(mask)
        large = [p for p in parts if p[3]-p[1] >= 5 and p[4] >= 6]
        if len(large) != 2:
            return False
        a, b = sorted(large)
        aw, ah, bw, bh = a[2]-a[0], a[3]-a[1], b[2]-b[0], b[3]-b[1]
        return bool(.15 <= aw/ah <= .65 and .15 <= bw/bh <= .65
            and a[4]/(aw*ah) >= .7 and b[4]/(bw*bh) >= .7
            and abs(ah-bh) <= max(2, ah*.2) and abs(aw-bw) <= max(2, aw*.3)
            and abs(a[1]-b[1]) <= max(2, ah*.15) and 1 <= b[0]-a[2] <= max(aw, bw)*1.6
            and abs((a[0]+b[2])/2-width/2) <= width*.15)

    @staticmethod
    def _clock(mask):
        parts = _components(mask)
        if not parts:
            return False
        h = max(p[3] for p in parts)-min(p[1] for p in parts)
        # Tiny HUD digits may break into several strokes after rasterisation.
        major = [p for p in parts if p[3]-p[1] >= h*.4]
        dots = [p for p in parts if p[3]-p[1] <= max(4, h*.4) and p[2]-p[0] <= max(4, h*.4)]
        colon = any(abs((a[0]+a[2])-(b[0]+b[2])) <= 4
                    and h*.15 <= abs((a[1]+a[3]-b[1]-b[3])/2) <= h*.7
                    for i, a in enumerate(dots) for b in dots[i+1:])
        span = max(p[2] for p in parts)-min(p[0] for p in parts)
        # At small resolutions the colon can touch a neighbouring digit.
        # Keep recognising the centred, wide numeric line in that case.
        return bool(len(major) >= 2 and (colon or span >= h*1.25))

    def analyze(self, image):
        if image is None or image.isNull():
            return TimerView()
        changed = self.size is not None and self.size != (image.width(), image.height())
        if changed:
            self.box = None
        self.size = image.width(), image.height()
        # Preserve aspect ratio; fixed minimum screen-pixel crops distorted
        # small HUD glyphs at 720x480 and with small hud_scaling values.
        w = max(8, round(image.width()*96/image.height()))
        sample = image.scaled(w, 96, Qt.IgnoreAspectRatio, Qt.FastTransformation).convertToFormat(QImage.Format_RGBA8888)
        rgb = np.frombuffer(sample.constBits(), dtype=np.uint8).reshape(96, w, 4)[:, :, :3].astype(np.int16)
        red = (rgb[:, :, 0] >= 115) & (rgb[:, :, 0]-rgb[:, :, 1] >= 65) & (rgb[:, :, 0]-rgb[:, :, 2] >= 50)
        white = (rgb.min(axis=2) >= 150) & (rgb.max(axis=2)-rgb.min(axis=2) <= 85)
        # The timer is the first HUD row. The old 10%-high search could pick
        # stationary red scores, names or equipment below a moving white clock.
        for mask in (red, white):
            mask[:, :round(w*.16)] = False
            mask[:, round(w*.84):] = False
            mask[35:] = False
        candidates = []
        for mask, colored in ((red, True), (white, False)):
            if mask.mean() > .35:
                continue
            rows = np.flatnonzero(mask.sum(axis=1))
            if not len(rows):
                continue
            runs = np.split(rows, np.flatnonzero(np.diff(rows) > 3)+1)
            for run in runs:
                top, bottom = int(run[0]), int(run[-1])+1
                if not 5 <= bottom-top <= 65:
                    continue
                line = mask[top:bottom]
                ys, xs = np.nonzero(line)
                if not len(xs) or abs((xs.min()+xs.max())/2-w/2) > w*.18:
                    continue
                label = 'bars' if colored and self._bars(line) else 'clock' if self._clock(line) else 'unknown'
                if label != 'unknown':
                    margin = max(1, round((bottom-top)*.20))
                    box = (round(w*.12), max(0, top-margin), round(w*.88), min(96, bottom+margin))
                    candidates.append((top, box, colored, label))
        if not candidates:
            return TimerView(layout_changed=changed)
        # Select by HUD position, never by red colour alone.
        _, chosen, is_red, kind = min(candidates, key=lambda item: item[0])
        if self.box is None or abs(self.box[1]-chosen[1]) > 8 or abs(self.box[3]-chosen[3]) > 8:
            changed |= self.box is not None
            self.box = chosen
        left, top, right, bottom = self.box
        mask = (red if is_red else white)[top:bottom, left:right]
        if not mask.any():
            # A tiny HUD mark can leave the cached crop before exceeding the
            # usual jitter threshold. Use the current, validated candidate.
            self.box = chosen
            changed = True
            left, top, right, bottom = chosen
            mask = (red if is_red else white)[top:bottom, left:right]
        raw = (mask.astype(np.uint8)*255).copy()
        glyph = QImage(raw.data, raw.shape[1], raw.shape[0], raw.shape[1], QImage.Format_Grayscale8).copy()
        # OCR needs a tight text line with a little breathing room. A wide
        # nearly-empty image causes Windows OCR to drop tiny HUD characters.
        ys, xs = np.nonzero(raw)
        text_pixels = np.pad(raw[ys.min():ys.max()+1, xs.min():xs.max()+1], 3).copy()
        clock_image = QImage(text_pixels.data, text_pixels.shape[1], text_pixels.shape[0],
                             text_pixels.shape[1], QImage.Format_Grayscale8).copy()
        glyph = glyph.scaled(64, 24, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        # QImage grayscale rows may be padded; width 64 is naturally aligned.
        pattern = np.frombuffer(glyph.constBits(), dtype=np.uint8).reshape(24, 64).copy().astype(np.float32)/255
        return TimerView(kind, is_red, pattern, bool(changed), clock_image)


def same_timer(a, b):
    if a is None or b is None:
        return False
    return bool(np.abs(a-b).sum()/max(1, np.maximum(a, b).sum()) < .065)


class PauseTimerDetector:
    def __init__(self):
        self.paused = False
        self.clear_observations()

    def clear_observations(self):
        self.reference = self.candidate = None
        self.still_since = self.candidate_at = None
        self.bars_since = None
        self.red_since = None
        self.verified_seconds = self.verified_at = None

    def suspend(self):
        # Hidden/obscured UI never ends an established pause.
        self.candidate = None; self.candidate_at = None; self.bars_since = None
        self.verified_seconds = self.verified_at = None
        if not self.paused:
            self.reference = None; self.still_since = None
            self.red_since = None

    def seed(self, paused):
        if paused != self.paused:
            self.paused = paused
            self.clear_observations()

    def needs_clock_verification(self, view, now):
        return (not self.paused and view.kind == 'clock' and view.red
                and not view.layout_changed
                and self.red_since is not None and now-self.red_since >= 3
                and same_timer(self.reference, view.pattern))

    def observe(self, view, now, clock_seconds=None):
        if view.layout_changed:
            self.clear_observations()
        if view.kind == 'bars' and view.red:
            self.reference = None; self.still_since = None
            self.red_since = None
            self.verified_seconds = self.verified_at = None
            if self.bars_since is None:
                self.bars_since = now
            if now-self.bars_since >= .25:
                self.paused = True
                return True
            return None
        self.bars_since = None
        if view.kind != 'clock':
            self.suspend()
            return None
        if self.reference is None:
            self.reference = view.pattern; self.still_since = now
            self.red_since = now if view.red else None
            return None
        if same_timer(self.reference, view.pattern):
            self.candidate = None; self.candidate_at = None
            if not view.red:
                self.red_since = None
                self.verified_seconds = self.verified_at = None
            elif self.red_since is None:
                self.red_since = now
            if self.paused:
                return None
            if self.red_since is not None and now-self.red_since >= 3:
                # A stationary red shape is only a candidate. Require two
                # actual M:SS reads to exclude a score/name/weapon HUD fragment.
                valid = isinstance(clock_seconds, int) and not isinstance(clock_seconds, bool) and 0 <= clock_seconds < 3600
                if valid:
                    if (clock_seconds == self.verified_seconds and self.verified_at is not None
                            and .4 <= now-self.verified_at <= 2):
                        self.paused = True
                        return True
                    self.verified_seconds, self.verified_at = clock_seconds, now
            return None
        self.verified_seconds = self.verified_at = None
        if self.candidate is None or not same_timer(self.candidate, view.pattern):
            self.candidate, self.candidate_at = view.pattern, now
            return None
        if now-self.candidate_at >= .35:
            self.reference = view.pattern; self.still_since = now
            self.red_since = now if view.red else None
            self.candidate = None; self.candidate_at = None
            if self.paused:
                self.paused = False
                return False
        return None
