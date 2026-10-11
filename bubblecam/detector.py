"""Motion analysis on a shrunken grey frame.

Per frame it produces:
  zones  - fraction of each watch zone covered by moving blobs
  zone   - the highest of those (the pleco signal)
  tank   - fraction of the whole (non ignored) frame covered by moving blobs
  boxes  - bounding boxes of moving blobs, for drawing on the live view
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from .config import DetectConfig


@dataclass
class Scores:
    zone: float = 0.0
    tank: float = 0.0
    zones: dict[str, float] = field(default_factory=dict)
    boxes: list[tuple[float, float, float, float]] = field(default_factory=list)  # fractions of frame
    light_change: bool = False
    settling: bool = False


class Detector:
    def __init__(self, cfg: DetectConfig, frame_w: int, frame_h: int):
        self.cfg = cfg
        self.w = cfg.analysis_width
        self.h = max(1, round(frame_h * self.w / frame_w))
        self.bg: np.ndarray | None = None
        self.brightness: float | None = None
        self.settle_until = 0.0
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self.set_zones([])

    def set_zones(self, zones: list[dict]) -> None:
        w, h = self.w, self.h

        def rect(z):
            x0, y0 = int(z["x"] * w), int(z["y"] * h)
            x1, y1 = int((z["x"] + z["w"]) * w), int((z["y"] + z["h"]) * h)
            return x0, y0, max(x1, x0 + 1), max(y1, y0 + 1)

        self.valid = np.ones((h, w), np.uint8)
        for z in zones:
            if z["kind"] == "ignore":
                x0, y0, x1, y1 = rect(z)
                self.valid[y0:y1, x0:x1] = 0
        self.valid_count = max(1, int(self.valid.sum()))
        self.watch = []
        for z in zones:
            if z["kind"] == "watch":
                x0, y0, x1, y1 = rect(z)
                m = np.zeros((h, w), np.uint8)
                m[y0:y1, x0:x1] = 1
                m &= self.valid
                self.watch.append((z["name"], m, max(1, int(m.sum()))))

    def analyze(self, frame: np.ndarray, ts: float) -> Scores:
        cfg = self.cfg
        small = cv2.resize(frame, (self.w, self.h), interpolation=cv2.INTER_AREA)
        small = cv2.GaussianBlur(small, (5, 5), 0)
        smallf = small.astype(np.float32)
        mean = float(smallf.mean())

        scores = Scores(zones={name: 0.0 for name, _, _ in self.watch})

        light_change = self.brightness is not None and abs(mean - self.brightness) > cfg.light_jump
        self.brightness = mean if self.brightness is None else 0.9 * self.brightness + 0.1 * mean
        if self.bg is None or light_change:
            # Tank light switched (or first frame): start the background over and hold off on events
            # while auto adjustments and the fish settle.
            self.bg = smallf.copy()
            self.brightness = mean
            if light_change:
                self.settle_until = ts + cfg.light_settle_seconds
                scores.light_change = True
            scores.settling = True
            return scores

        diff = cv2.absdiff(smallf, self.bg)
        cv2.accumulateWeighted(smallf, self.bg, cfg.background_alpha)
        mask = (diff > cfg.diff_threshold).astype(np.uint8)
        mask &= self.valid
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self.kernel)

        n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        keep = np.zeros(n, bool)
        keep[1:] = stats[1:, cv2.CC_STAT_AREA] >= cfg.min_blob_area
        moving = keep[labels].astype(np.uint8)

        scores.tank = float(moving.sum()) / self.valid_count
        for name, m, count in self.watch:
            scores.zones[name] = float((moving & m).sum()) / count
        scores.zone = max(scores.zones.values(), default=0.0)
        for i in np.nonzero(keep)[0]:
            x, y, bw, bh = stats[i, :4]
            scores.boxes.append((x / self.w, y / self.h, bw / self.w, bh / self.h))
        scores.settling = ts < self.settle_until
        return scores
