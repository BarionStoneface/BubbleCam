"""Tanks and the animals spotlighted in them.

Every box (spotlight areas, home box, ignore areas) is stored in the tank's *reference* picture
coordinates, as fractions of the frame. When the camera sits a little differently from when the
boxes were drawn, the view tracker measures the shift as an affine transform and the boxes are moved
to match before the detector uses them.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from .db import DB

MAX_SPOTLIGHTS = 4  # per tank; each gets a column in the per frame recording sidecar

# Sensitivity 1 (calm, fewer events) .. 5 (eager). 3 is the default.
SPOT_THRESHOLD = {1: 0.03, 2: 0.018, 3: 0.01, 4: 0.006, 5: 0.004}
SPOT_START_S = {1: 6.0, 2: 4.0, 3: 3.0, 4: 2.0, 5: 1.5}
BURST_RATIO = {1: 5.0, 2: 4.0, 3: 3.0, 4: 2.5, 5: 2.0}

DEFAULT_SETTINGS = {
    "ignore": [],
    "spot_sensitivity": 3,
    "burst_sensitivity": 3,
    "feeding_detection": True,
}

IDENTITY = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])


# Geometry. Rects are dicts with x, y, w, h as fractions of the frame.

def transform_rect(rect: dict, A: np.ndarray) -> dict:
    """Move a rect by an affine transform in normalised coordinates; returns the bounding box, clipped."""
    x, y, w, h = rect["x"], rect["y"], rect["w"], rect["h"]
    pts = np.array([[x, y, 1], [x + w, y, 1], [x, y + h, 1], [x + w, y + h, 1]])
    out = pts @ A.T
    x0, y0 = np.clip(out.min(axis=0), 0, 1)
    x1, y1 = np.clip(out.max(axis=0), 0, 1)
    return {**rect, "x": float(x0), "y": float(y0), "w": float(max(0.0, x1 - x0)), "h": float(max(0.0, y1 - y0))}


def invert(A: np.ndarray) -> np.ndarray:
    M = np.vstack([A, [0, 0, 1]])
    return np.linalg.inv(M)[:2]


def clean_rect(r: dict | None) -> dict | None:
    if not r:
        return None
    x, y = min(max(float(r["x"]), 0.0), 1.0), min(max(float(r["y"]), 0.0), 1.0)
    w, h = min(max(float(r["w"]), 0.0), 1.0 - x), min(max(float(r["h"]), 0.0), 1.0 - y)
    if w < 0.005 or h < 0.005:
        return None
    return {"x": x, "y": y, "w": w, "h": h}


def rects_close(a: dict | None, b: dict | None, tol: float = 0.02) -> bool:
    if a is None or b is None:
        return a is b
    return all(abs(a[k] - b[k]) <= tol for k in ("x", "y", "w", "h"))


@dataclass
class Spotlight:
    id: int
    tank_id: int
    name: str
    slot: int
    areas: list[dict]
    home: dict | None

    def to_dict(self, A: np.ndarray = IDENTITY) -> dict:
        return {"id": self.id, "name": self.name, "slot": self.slot,
                "areas": [transform_rect(a, A) for a in self.areas],
                "home": transform_rect(self.home, A) if self.home else None}


@dataclass
class Tank:
    id: int
    name: str
    settings: dict
    spotlights: list[Spotlight] = field(default_factory=list)

    def setting(self, key: str):
        return self.settings.get(key, DEFAULT_SETTINGS[key])

    @property
    def zone_threshold(self) -> float:
        return SPOT_THRESHOLD[int(self.setting("spot_sensitivity"))]

    @property
    def zone_start_seconds(self) -> float:
        return SPOT_START_S[int(self.setting("spot_sensitivity"))]

    @property
    def burst_ratio(self) -> float:
        return BURST_RATIO[int(self.setting("burst_sensitivity"))]

    def spotlight(self, spot_id: int) -> Spotlight | None:
        return next((s for s in self.spotlights if s.id == spot_id), None)

    def to_dict(self, A: np.ndarray = IDENTITY) -> dict:
        settings = {**DEFAULT_SETTINGS, **self.settings}
        settings["ignore"] = [transform_rect(r, A) for r in settings["ignore"]]
        return {"id": self.id, "name": self.name, "settings": settings,
                "spotlights": [s.to_dict(A) for s in self.spotlights]}


class TankStore:
    """Thread safe access to tanks and spotlights. Listeners hear about every change."""

    def __init__(self, db: DB):
        self.db = db
        self.lock = threading.RLock()
        self.listeners: list = []
        self.tanks: dict[int, Tank] = {}
        self.reload()

    def reload(self) -> None:
        with self.lock:
            tanks = {}
            for r in self.db.query("SELECT * FROM tanks ORDER BY id"):
                tanks[r["id"]] = Tank(r["id"], r["name"], json.loads(r["settings"] or "{}"))
            for r in self.db.query("SELECT * FROM spotlights ORDER BY slot"):
                if r["tank_id"] in tanks:
                    tanks[r["tank_id"]].spotlights.append(Spotlight(
                        r["id"], r["tank_id"], r["name"], r["slot"], json.loads(r["areas"] or "[]"),
                        json.loads(r["home"]) if r["home"] else None))
            self.tanks = tanks
        for fn in self.listeners:
            fn()

    def get(self, tank_id: int | None) -> Tank | None:
        return self.tanks.get(tank_id) if tank_id is not None else None

    def all(self) -> list[Tank]:
        return list(self.tanks.values())

    # Tanks

    def create(self, name: str) -> Tank:
        name = name.strip() or f"Tank {len(self.tanks) + 1}"
        tank_id = self.db.execute("INSERT INTO tanks(name, created_ts, settings) VALUES (?,?,?)",
                                  (name, time.time(), json.dumps({}))).lastrowid
        self.reload()
        return self.tanks[tank_id]

    def rename(self, tank_id: int, name: str) -> None:
        if name.strip():
            self.db.execute("UPDATE tanks SET name=? WHERE id=?", (name.strip(), tank_id))
            self.reload()

    def delete(self, tank_id: int) -> None:
        self.db.execute("DELETE FROM spotlights WHERE tank_id=?", (tank_id,))
        self.db.execute("DELETE FROM tanks WHERE id=?", (tank_id,))
        self.reload()

    def update_settings(self, tank_id: int, changes: dict) -> None:
        with self.lock:
            tank = self.tanks[tank_id]
            settings = dict(tank.settings)
            for key, value in changes.items():
                if key not in DEFAULT_SETTINGS:
                    raise ValueError(f"unknown setting: {key}")
                if key in ("spot_sensitivity", "burst_sensitivity"):
                    value = int(value)
                    if value not in SPOT_THRESHOLD:
                        raise ValueError("sensitivity is 1 to 5")
                elif key == "feeding_detection":
                    value = bool(value)
                elif key == "ignore":
                    value = [r for r in (clean_rect(x) for x in value) if r]
                settings[key] = value
            self.db.execute("UPDATE tanks SET settings=? WHERE id=?", (json.dumps(settings), tank_id))
        self.reload()

    # Spotlights

    def add_spotlight(self, tank_id: int, name: str) -> Spotlight:
        with self.lock:
            tank = self.tanks[tank_id]
            used = {s.slot for s in tank.spotlights}
            free = [i for i in range(MAX_SPOTLIGHTS) if i not in used]
            if not free:
                raise ValueError(f"a tank can spotlight up to {MAX_SPOTLIGHTS} animals")
            spot_id = self.db.execute(
                "INSERT INTO spotlights(tank_id, name, slot, areas, created_ts) VALUES (?,?,?,?,?)",
                (tank_id, name.strip() or "Unnamed", free[0], "[]", time.time())).lastrowid
        self.reload()
        return self.tanks[tank_id].spotlight(spot_id)

    def update_spotlight(self, spot_id: int, name: str | None = None, areas: list | None = None,
                         home: dict | None | bool = False) -> None:
        """home=False means leave it; None clears it."""
        sets, args = [], []
        if name is not None and name.strip():
            sets.append("name=?")
            args.append(name.strip())
        if areas is not None:
            sets.append("areas=?")
            args.append(json.dumps([r for r in (clean_rect(a) for a in areas) if r]))
        if home is not False:
            sets.append("home=?")
            r = clean_rect(home) if home else None
            args.append(json.dumps(r) if r else None)
        if sets:
            self.db.execute(f"UPDATE spotlights SET {', '.join(sets)} WHERE id=?", (*args, spot_id))
            self.reload()

    def delete_spotlight(self, spot_id: int) -> None:
        self.db.execute("DELETE FROM spotlights WHERE id=?", (spot_id,))
        self.reload()
