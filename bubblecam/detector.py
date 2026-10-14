"""Motion analysis on a shrunken grey frame.

Per frame it produces:
  spots     - for each spotlighted animal, the fraction of its areas covered by moving blobs
  zone      - the highest of those
  tank      - fraction of the whole (non ignored) picture covered by moving blobs
  top_share - how much of the movement is in the top quarter of the picture (fish rushing up to food)
  bubbles   - how much of the filter bubble box changed since the previous frame (None without a box)
  boxes     - moving blobs, for drawing on the live view
  blobs     - the same blobs with centre and size, for tracking: (cx, cy, w, h, area) as fractions
  centroids - per spotlighted animal, where its movement is centred (None when it isn't moving)
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from .config import DetectConfig

TOP_BAND = 0.25
# More than this much of the picture moving at once isn't fish: the camera or the light is being changed.
DISTURBANCE = 0.45


@dataclass
class Scores:
    zone: float = 0.0
    tank: float = 0.0
    top_share: float = 0.0
    spots: dict[int, float] = field(default_factory=dict)
    boxes: list[tuple[float, float, float, float]] = field(default_factory=list)  # fractions of frame
    blobs: list[tuple[float, float, float, float, float]] = field(default_factory=list)
    mask: np.ndarray | None = None      # the moving pixels, at analysis size (for heatmaps)
    centroids: dict[int, tuple[float, float] | None] = field(default_factory=dict)
    bubbles: float | None = None
    brightness: float = 0.0
    light_change: bool = False
    disturbed: bool = False
    settling: bool = False


class Detector:
    def __init__(self, cfg: DetectConfig, frame_w: int, frame_h: int):
        self.cfg = cfg
        self.w = cfg.analysis_width
        self.h = max(1, round(frame_h * self.w / frame_w))
        self.bg: np.ndarray | None = None
        self.prev: np.ndarray | None = None
        self.brightness: float | None = None
        self.settle_until = 0.0
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        top = np.zeros((self.h, self.w), np.uint8)
        top[: max(1, int(self.h * TOP_BAND))] = 1
        self.top_mask = top
        self.set_layout({}, [])

    def _rect(self, z: dict) -> tuple[int, int, int, int]:
        w, h = self.w, self.h
        x0, y0 = int(z["x"] * w), int(z["y"] * h)
        x1, y1 = int((z["x"] + z["w"]) * w), int((z["y"] + z["h"]) * h)
        return x0, y0, max(x1, x0 + 1), max(y1, y0 + 1)

    def set_layout(self, spots: dict[int, list[dict]], ignore: list[dict], bubbles: dict | None = None) -> None:
        """spots: spotlight id -> its areas; ignore: areas to cut out; bubbles: the filter bubble box.
        All in current view coordinates."""
        self.bubble_box = self._rect(bubbles) if bubbles else None
        valid = np.ones((self.h, self.w), np.uint8)
        for z in ignore:
            x0, y0, x1, y1 = self._rect(z)
            valid[y0:y1, x0:x1] = 0
        watch = []
        for spot_id, areas in spots.items():
            m = np.zeros((self.h, self.w), np.uint8)
            for z in areas:
                x0, y0, x1, y1 = self._rect(z)
                m[y0:y1, x0:x1] = 1
            m &= valid
            if m.any():
                watch.append((spot_id, m, int(m.sum())))
        # Swap in one go: the capture thread may be mid analysis.
        self.valid, self.valid_count, self.watch = valid, max(1, int(valid.sum())), watch

    def restart(self, ts: float, settle_seconds: float) -> None:
        """Forget the background (camera moved, tank switched) and hold off events for a moment."""
        self.bg = None
        self.settle_until = max(self.settle_until, ts + settle_seconds)

    def analyze(self, frame: np.ndarray, ts: float) -> Scores:
        cfg = self.cfg
        valid, valid_count, watch = self.valid, self.valid_count, self.watch
        small = cv2.resize(frame, (self.w, self.h), interpolation=cv2.INTER_AREA)
        small = cv2.GaussianBlur(small, (5, 5), 0)
        smallf = small.astype(np.float32)
        mean = float(smallf.mean())

        scores = Scores(spots={spot_id: 0.0 for spot_id, _, _ in watch}, brightness=mean)

        # Bubbles: plain frame to frame change inside the box. A running bubble stream never holds still;
        # the background model would slowly learn a constant stream as "normal", frame differences don't.
        box = self.bubble_box
        if box is not None and self.prev is not None:
            x0, y0, x1, y1 = box
            d = cv2.absdiff(smallf[y0:y1, x0:x1], self.prev[y0:y1, x0:x1])
            scores.bubbles = float((d > cfg.diff_threshold).mean()) if d.size else 0.0
        self.prev = smallf

        light_change = self.brightness is not None and abs(mean - self.brightness) > cfg.light_jump
        self.brightness = mean if self.brightness is None else 0.9 * self.brightness + 0.1 * mean
        if self.bg is None or light_change:
            # Tank light switched (or first frame): start the background over and hold off on events
            # while auto adjustments and the fish settle. Whatever was in this first frame is baked
            # into the background and fades out over the next seconds looking like a slow mover, so the
            # hold off covers that too.
            self.bg = smallf.copy()
            self.brightness = mean
            self.settle_until = max(self.settle_until, ts + cfg.light_settle_seconds)
            if light_change:
                scores.light_change = True
            scores.settling = True
            return scores

        diff = cv2.absdiff(smallf, self.bg)
        cv2.accumulateWeighted(smallf, self.bg, cfg.background_alpha)
        mask = (diff > cfg.diff_threshold).astype(np.uint8)
        mask &= valid
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self.kernel)

        n, labels, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
        keep = np.zeros(n, bool)
        keep[1:] = stats[1:, cv2.CC_STAT_AREA] >= cfg.min_blob_area
        moving = keep[labels].astype(np.uint8)

        total = int(moving.sum())
        scores.tank = total / valid_count
        scores.mask = moving
        if scores.tank > DISTURBANCE:
            scores.disturbed = True
            self.restart(ts, 10.0)
            scores.settling = True
            return scores
        scores.top_share = float((moving & self.top_mask).sum()) / total if total else 0.0
        for spot_id, m, count in watch:
            inside = moving & m
            hits = int(inside.sum())
            scores.spots[spot_id] = hits / count
            if hits:
                mo = cv2.moments(inside, binaryImage=True)
                scores.centroids[spot_id] = (mo["m10"] / mo["m00"] / self.w, mo["m01"] / mo["m00"] / self.h)
            else:
                scores.centroids[spot_id] = None
        scores.zone = max(scores.spots.values(), default=0.0)
        for i in np.nonzero(keep)[0]:
            x, y, bw, bh = stats[i, :4]
            scores.boxes.append((x / self.w, y / self.h, bw / self.w, bh / self.h))
            scores.blobs.append((cents[i][0] / self.w, cents[i][1] / self.h, bw / self.w, bh / self.h,
                                 stats[i, cv2.CC_STAT_AREA] / (self.w * self.h)))
        scores.settling = ts < self.settle_until
        return scores
