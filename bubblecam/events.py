"""Turns per frame motion scores into events: pleco zone visits and whole tank bursts."""

from __future__ import annotations

import collections
import logging
from pathlib import Path

import cv2
import numpy as np

from .config import BurstEventConfig, ZoneEventConfig
from .db import DB
from .detector import Scores

log = logging.getLogger(__name__)

# Movement that drops out for less than this still counts as continuous while an event is warming up.
# A pleco moves in shuffles with pauses; a guppy crossing is one clean pass.
GAP_TOLERANCE = 1.5


class Hysteresis:
    """Starts after `start_s` of (mostly) continuous activity, ends after `end_s` of quiet."""

    def __init__(self, start_s: float, end_s: float):
        self.start_s, self.end_s = start_s, end_s
        self.active = False
        self.candidate_since: float | None = None
        self.last_true = 0.0
        self.peak = 0.0
        self.peak_ts = 0.0
        self.peak_frame: np.ndarray | None = None

    def update(self, on: bool, ts: float, value: float, frame: np.ndarray) -> str | None:
        if on:
            if not self.active and (self.candidate_since is None or ts - self.last_true > GAP_TOLERANCE):
                self.candidate_since = ts
            self.last_true = ts
            if value > self.peak:
                self.peak, self.peak_ts = value, ts
                self.peak_frame = _thumb(frame)
        if not self.active:
            if on and ts - self.candidate_since >= self.start_s:
                self.active = True
                return "start"
            if not on and self.candidate_since is not None and ts - self.last_true > GAP_TOLERANCE:
                self._reset()
            return None
        if not on and ts - self.last_true >= self.end_s:
            self.active = False
            return "end"
        return None

    def _reset(self) -> None:
        self.candidate_since = None
        self.peak = 0.0
        self.peak_frame = None


def _thumb(frame: np.ndarray, width: int = 480) -> np.ndarray:
    h = round(frame.shape[0] * width / frame.shape[1])
    return cv2.resize(frame, (width, h), interpolation=cv2.INTER_AREA)


class Baseline:
    """Rolling median of whole tank movement, sampled once a second."""

    def __init__(self, minutes: float):
        self.samples: collections.deque[float] = collections.deque(maxlen=max(10, int(minutes * 60)))
        self.last_sample = 0.0
        self.acc: list[float] = []
        self.value = 0.0

    def update(self, ts: float, tank: float) -> float:
        self.acc.append(tank)
        if ts - self.last_sample >= 1.0:
            self.samples.append(float(np.mean(self.acc)))
            self.acc.clear()
            self.last_sample = ts
            self.value = float(np.median(self.samples))
        return self.value


class EventManager:
    def __init__(self, zone_cfg: ZoneEventConfig, burst_cfg: BurstEventConfig, db: DB, thumbs_dir: Path):
        self.zcfg, self.bcfg = zone_cfg, burst_cfg
        self.db = db
        self.thumbs_dir = thumbs_dir
        thumbs_dir.mkdir(parents=True, exist_ok=True)
        self.zone_trackers: dict[str, Hysteresis] = {}
        self.burst = Hysteresis(burst_cfg.start_seconds, burst_cfg.end_seconds)
        self.baseline = Baseline(burst_cfg.baseline_minutes)
        self.open_ids: dict[str, int] = {}
        # Called as fn(kind, zone, ts) when an event starts.
        self.listeners: list = []

    def update(self, scores: Scores, ts: float, frame: np.ndarray) -> None:
        for name, value in scores.zones.items():
            tr = self.zone_trackers.get(name)
            if tr is None:
                tr = self.zone_trackers[name] = Hysteresis(self.zcfg.start_seconds, self.zcfg.end_seconds)
            on = value >= self.zcfg.threshold and not scores.settling
            self._handle(f"zone:{name}", "zone", name, tr, tr.update(on, ts, value, frame),
                         self.zcfg.pre_roll, self.zcfg.post_roll)

        if self.bcfg.enabled:
            base = self.baseline.update(ts, scores.tank)
            limit = max(base * self.bcfg.ratio, base + self.bcfg.floor)
            on = scores.tank > limit and not scores.settling
            self._handle("burst", "burst", None, self.burst, self.burst.update(on, ts, scores.tank, frame),
                         self.bcfg.pre_roll, self.bcfg.post_roll)

    def _handle(self, key: str, kind: str, zone: str | None, tr: Hysteresis, change: str | None,
                pre: float, post: float) -> None:
        if change == "start":
            self.open_ids[key] = self.db.open_event(kind, zone, tr.candidate_since - pre)
            log.info("event start: %s", key)
            for fn in self.listeners:
                try:
                    fn(kind, zone, tr.candidate_since)
                except Exception:
                    log.exception("event listener failed")
        elif change == "end":
            ev_id = self.open_ids.pop(key, None)
            if ev_id is None:
                tr._reset()
                return
            thumb = None
            if tr.peak_frame is not None:
                thumb = str(self.thumbs_dir / f"{ev_id}.jpg")
                cv2.imwrite(thumb, tr.peak_frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            self.db.close_event(ev_id, tr.last_true + post, tr.peak, tr.peak_ts, thumb)
            log.info("event end: %s (peak %.3f)", key, tr.peak)
            tr._reset()

    def close_all(self, ts: float) -> None:
        for key, ev_id in list(self.open_ids.items()):
            self.db.close_event(ev_id, ts, 0.0, ts, None)
        self.open_ids.clear()
