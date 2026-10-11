"""Is he home? For each spotlighted animal with a home box (a cave mouth, a favourite perch).

You teach it from the tank page: tap "home" when the animal is there and "away" when it isn't. Each tap
saves a small picture of the home box. Every couple of seconds the current picture is compared with the
saved ones and whichever label has the closest example wins. Examples taken under similar brightness are
preferred, so day and night examples don't get mixed up. A state only flips when most recent checks agree.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .config import PresenceConfig
from .db import DB
from .tanks import rects_close

log = logging.getLogger(__name__)

PATCH = 48
MAX_SAMPLES = 200
BRIGHTNESS_MATCH = 25.0


def patch(frame: np.ndarray, rect: dict) -> tuple[np.ndarray, float]:
    """Normalised small picture of the box, and its raw brightness."""
    h, w = frame.shape[:2]
    x0, y0 = int(rect["x"] * w), int(rect["y"] * h)
    x1, y1 = max(x0 + 2, int((rect["x"] + rect["w"]) * w)), max(y0 + 2, int((rect["y"] + rect["h"]) * h))
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
    return "home" if dist[labels == "home"].min() <= dist[labels == "away"].min() else "away"


class PresenceModel:
    """Examples and current state for one animal."""

    def __init__(self, cfg: PresenceConfig, db: DB, tank_id: int, spot_id: int, path: Path):
        self.cfg, self.db = cfg, db
        self.tank_id, self.spot_id = tank_id, spot_id
        self.path = path
        self.recent: collections.deque[str] = collections.deque(maxlen=cfg.window)
        self.home_rect: dict | None = None
        last = db.one("SELECT state, ts FROM presence WHERE spot_id=? ORDER BY ts DESC LIMIT 1", (spot_id,))
        self.state = last["state"] if last else "unknown"
        self.since = last["ts"] if last else None
        if path.exists():
            d = np.load(path)
            self.patches, self.labels, self.bright = d["patches"], d["labels"], d["bright"]
        else:
            self.clear(save=False)

    def clear(self, save: bool = True) -> None:
        self.patches = np.zeros((0, PATCH, PATCH), np.float32)
        self.labels = np.zeros(0, "<U4")
        self.bright = np.zeros(0, np.float32)
        self.recent.clear()
        if save:
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.stem + ".tmp.npz")
        np.savez(tmp, patches=self.patches, labels=self.labels, bright=self.bright)
        tmp.replace(self.path)

    def counts(self) -> dict:
        return {"home": int((self.labels == "home").sum()), "away": int((self.labels == "away").sum())}

    @property
    def ready(self) -> bool:
        c = self.counts()
        return self.home_rect is not None and c["home"] > 0 and c["away"] > 0


class PresenceManager:
    def __init__(self, cfg: PresenceConfig, db: DB, data_dir: Path):
        self.cfg = cfg
        self.db = db
        self.dir = data_dir / "tanks"
        self.lock = threading.Lock()
        self.models: dict[int, PresenceModel] = {}
        self.stored_home: dict[int, dict | None] = {}
        self.last_check = 0.0
        self.listeners: list[Callable[[int, int, str, float], None]] = []   # (tank, spot, state, ts)

    def _model(self, tank_id: int, spot_id: int) -> PresenceModel:
        m = self.models.get(spot_id)
        if m is None:
            m = self.models[spot_id] = PresenceModel(
                self.cfg, self.db, tank_id, spot_id, self.dir / str(tank_id) / f"presence_{spot_id}.npz")
        return m

    def set_layout(self, tank, A_rects: dict[int, dict | None]) -> None:
        """tank: the active Tank (or None). A_rects: spot id -> home box in current view coordinates."""
        with self.lock:
            if tank is None:
                for m in self.models.values():
                    m.home_rect = None
                return
            for spot in tank.spotlights:
                m = self._model(tank.id, spot.id)
                # Examples only make sense for the box they were taken from (stored coordinates).
                old = self.stored_home.get(spot.id, spot.home)
                if len(m.labels) and not rects_close(old, spot.home):
                    log.info("home box for %s changed; clearing its examples", spot.name)
                    m.clear()
                self.stored_home[spot.id] = spot.home
                m.home_rect = A_rects.get(spot.id)
            active = {s.id for s in tank.spotlights}
            for spot_id, m in self.models.items():
                if spot_id not in active:
                    m.home_rect = None

    def add_sample(self, tank_id: int, spot_id: int, frame: np.ndarray, label: str) -> None:
        if label not in ("home", "away"):
            raise ValueError("label must be home or away")
        with self.lock:
            m = self._model(tank_id, spot_id)
            if m.home_rect is None:
                raise ValueError("draw a home box for this animal first")
            p, b = patch(frame, m.home_rect)
            m.patches = np.concatenate([m.patches, p[None]])[-MAX_SAMPLES:]
            m.labels = np.append(m.labels, label)[-MAX_SAMPLES:]
            m.bright = np.append(m.bright, np.float32(b))[-MAX_SAMPLES:]
            m.save()
            m.recent.clear()
            m.recent.extend([label] * self.cfg.window)
            # You just told it the answer: record it, but don't notify you about what you said.
            self._set_state(m, label, time.time(), notify=False)

    def clear(self, spot_id: int) -> None:
        with self.lock:
            if spot_id in self.models:
                self.models[spot_id].clear()

    def update(self, frame: np.ndarray, ts: float, settling: bool) -> None:
        if ts - self.last_check < self.cfg.interval_seconds or settling:
            return
        self.last_check = ts
        flips = []
        with self.lock:
            for m in self.models.values():
                if not m.ready:
                    continue
                p, b = patch(frame, m.home_rect)
                guess = classify(p, b, m.patches, m.labels, m.bright)
                if guess is None:
                    continue
                m.recent.append(guess)
                winner, n = collections.Counter(m.recent).most_common(1)[0]
                if n >= self.cfg.agree and winner != m.state:
                    flips.append((m, winner))
            for m, winner in flips:
                self._set_state(m, winner, ts)

    def _set_state(self, m: PresenceModel, state: str, ts: float, notify: bool = True) -> None:
        if state == m.state:
            return
        m.state, m.since = state, ts
        self.db.execute("INSERT OR REPLACE INTO presence VALUES (?, ?, ?, ?)", (m.spot_id, ts, m.tank_id, state))
        log.info("spotlight %s: %s", m.spot_id, state)
        if notify:
            for fn in self.listeners:
                try:
                    fn(m.tank_id, m.spot_id, state, ts)
                except Exception:
                    log.exception("presence listener failed")

    def status(self, spot_id: int) -> dict:
        m = self.models.get(spot_id)
        if m is None:
            return {"state": "unknown", "since": None, "has_home": False, "examples": {"home": 0, "away": 0}}
        return {"state": m.state if m.ready else "unknown", "since": m.since,
                "has_home": m.home_rect is not None, "examples": m.counts()}


def time_out(db: DB, spot_id: int, start_ts: float, end_ts: float) -> tuple[float, int]:
    """Seconds this animal spent away from home between start and end, and how many times it left."""
    before = db.one("SELECT state FROM presence WHERE spot_id=? AND ts < ? ORDER BY ts DESC LIMIT 1",
                    (spot_id, start_ts))
    rows = db.query("SELECT ts, state FROM presence WHERE spot_id=? AND ts >= ? AND ts < ? ORDER BY ts",
                    (spot_id, start_ts, end_ts))
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
