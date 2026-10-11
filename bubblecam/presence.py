"""Is he home? Compares the cave mouth to example pictures you've labelled.

You teach it from the Live tab: tap "He's home" when he's in the cave and "Cave empty" when he's
out. Each tap saves a small picture of the home zone. Every couple of seconds the current picture is
compared with the saved ones, and whichever label has the closest example wins. Examples taken under
similar brightness are preferred, so day and night examples don't get mixed up. The more examples,
the better it gets; a few of each, day and night, is plenty.
"""

from __future__ import annotations

import collections
import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .config import PresenceConfig
from .db import DB

log = logging.getLogger(__name__)

PATCH = 48
MAX_SAMPLES = 200
BRIGHTNESS_MATCH = 25.0


def home_zone(zones: list[dict]) -> dict | None:
    return next((z for z in zones if z["kind"] == "home"), None)


def patch(frame: np.ndarray, zone: dict) -> tuple[np.ndarray, float]:
    """Normalised small picture of the zone, and its raw brightness."""
    h, w = frame.shape[:2]
    x0, y0 = int(zone["x"] * w), int(zone["y"] * h)
    x1, y1 = max(x0 + 2, int((zone["x"] + zone["w"]) * w)), max(y0 + 2, int((zone["y"] + zone["h"]) * h))
    crop = cv2.resize(frame[y0:y1, x0:x1], (PATCH, PATCH), interpolation=cv2.INTER_AREA).astype(np.float32)
    mean = float(crop.mean())
    return (crop - mean) / (crop.std() + 5.0), mean


def classify(p: np.ndarray, brightness: float, patches: np.ndarray, labels: np.ndarray,
             bright: np.ndarray) -> str | None:
    if not ((labels == "home").any() and (labels == "away").any()):
        return None
    near = np.abs(bright - brightness) < BRIGHTNESS_MATCH
    # Use brightness matched examples only if both labels are represented among them.
    if (labels[near] == "home").any() and (labels[near] == "away").any():
        patches, labels = patches[near], labels[near]
    dist = np.abs(patches - p).mean(axis=(1, 2))
    best_home = dist[labels == "home"].min()
    best_away = dist[labels == "away"].min()
    return "home" if best_home <= best_away else "away"


class Presence:
    def __init__(self, cfg: PresenceConfig, db: DB, data_dir: Path):
        self.cfg = cfg
        self.db = db
        self.dir = data_dir / "presence"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.zone: dict | None = None
        self.last_check = 0.0
        self.recent: collections.deque[str] = collections.deque(maxlen=cfg.window)
        self.listeners: list[Callable[[str, float], None]] = []
        last = db.one("SELECT state, ts FROM presence ORDER BY ts DESC LIMIT 1")
        self.state: str = last["state"] if last else "unknown"
        self.since: float | None = last["ts"] if last else None
        self._load()

    # Examples

    def _load(self) -> None:
        f = self.dir / "samples.npz"
        meta = self.dir / "zone.json"
        self.sample_zone = json.loads(meta.read_text()) if meta.exists() else None
        if f.exists():
            d = np.load(f)
            self.patches, self.labels, self.bright = d["patches"], d["labels"], d["bright"]
        else:
            self.patches = np.zeros((0, PATCH, PATCH), np.float32)
            self.labels = np.zeros(0, "<U4")
            self.bright = np.zeros(0, np.float32)

    def _save(self) -> None:
        np.savez(self.dir / "samples.npz", patches=self.patches, labels=self.labels, bright=self.bright)
        (self.dir / "zone.json").write_text(json.dumps(self.zone))

    def set_zones(self, zones: list[dict]) -> None:
        with self.lock:
            self.zone = home_zone(zones)
            # Examples only make sense for the exact box they were taken from.
            if self.sample_zone and self.zone != self.sample_zone and len(self.labels):
                log.info("home zone moved; clearing presence examples")
                self._clear()
            self.sample_zone = self.zone

    def _clear(self) -> None:
        self.patches = self.patches[:0]
        self.labels = self.labels[:0]
        self.bright = self.bright[:0]
        self.recent.clear()
        self._save()

    def clear(self) -> None:
        with self.lock:
            self._clear()

    def add_sample(self, frame: np.ndarray, label: str) -> None:
        if label not in ("home", "away"):
            raise ValueError("label must be home or away")
        with self.lock:
            if not self.zone:
                raise ValueError("draw a Home zone over the cave mouth first")
            p, b = patch(frame, self.zone)
            self.patches = np.concatenate([self.patches, p[None]])[-MAX_SAMPLES:]
            self.labels = np.append(self.labels, label)[-MAX_SAMPLES:]
            self.bright = np.append(self.bright, np.float32(b))[-MAX_SAMPLES:]
            self.sample_zone = self.zone
            self._save()
            self.recent.clear()
            # You just told it the answer, so take it.
            self.recent.extend([label] * self.cfg.window)
        self._set_state(label, time.time(), notify=False)

    def counts(self) -> dict:
        return {"home": int((self.labels == "home").sum()), "away": int((self.labels == "away").sum())}

    # Live

    def update(self, frame: np.ndarray, ts: float, settling: bool) -> None:
        if ts - self.last_check < self.cfg.interval_seconds or settling:
            return
        self.last_check = ts
        with self.lock:
            if not self.zone:
                return
            p, b = patch(frame, self.zone)
            guess = classify(p, b, self.patches, self.labels, self.bright)
            if guess is None:
                return
            self.recent.append(guess)
            votes = collections.Counter(self.recent)
            winner, n = votes.most_common(1)[0]
        if n >= self.cfg.agree and winner != self.state:
            self._set_state(winner, ts)

    def _set_state(self, state: str, ts: float, notify: bool = True) -> None:
        if state == self.state:
            return
        self.state, self.since = state, ts
        self.db.execute("INSERT OR REPLACE INTO presence VALUES (?, ?)", (ts, state))
        log.info("presence: %s", state)
        if not notify:
            # You just told it yourself; no need to tell you back.
            return
        for fn in self.listeners:
            try:
                fn(state, ts)
            except Exception:
                log.exception("presence listener failed")

    def status(self) -> dict:
        c = self.counts()
        ready = c["home"] > 0 and c["away"] > 0
        return {"state": self.state if ready and self.zone else "unknown", "since": self.since,
                "has_zone": self.zone is not None, "examples": c}


def time_out(db: DB, start_ts: float, end_ts: float) -> tuple[float, int]:
    """Seconds spent out of the cave between start and end, and how many times he came out."""
    before = db.one("SELECT state FROM presence WHERE ts < ? ORDER BY ts DESC LIMIT 1", (start_ts,))
    rows = db.query("SELECT ts, state FROM presence WHERE ts >= ? AND ts < ? ORDER BY ts", (start_ts, end_ts))
    state = before["state"] if before else None
    t = start_ts
    total, outings = 0.0, 0
    for r in rows:
        if state == "away":
            total += r["ts"] - t
        if r["state"] == "away" and state != "away":
            outings += 1
        state, t = r["state"], r["ts"]
    if state == "away":
        total += max(0.0, min(end_ts, time.time()) - t)
    return total, outings
