"""Turns per frame motion scores into events: spotlight animal activity, whole tank bursts, and feeding."""

from __future__ import annotations

import collections
import logging
import json
import time
from pathlib import Path

import cv2
import numpy as np

from .config import BurstEventConfig, ChaseEventConfig, FeedingConfig, ZoneEventConfig
from .db import DB
from .detector import Scores
from .tracks import ChaseConfig, ChaseWatch, Tracker

# Something in a spotlighted animal's area that travels faster than this is a fish passing through, not
# the animal: plecos and shrimp cover well under a twentieth of the tank a second even when busy, while
# a cruising guppy covers more. Frame widths per second, measured over the last two seconds.
PASSER_SPEED = 0.06
SLOW_AGE = 2.0

log = logging.getLogger(__name__)

# Movement that drops out for less than this still counts as continuous while an event is warming up.
# A pleco moves in shuffles with pauses; a guppy crossing is one clean pass.
GAP_TOLERANCE = 1.5


class Hysteresis:
    """Starts after `start_s` of (mostly) continuous activity, ends after `end_s` of quiet."""

    def __init__(self, start_s: float, end_s: float, max_s: float = 600.0):
        self.start_s, self.end_s, self.max_s = start_s, end_s, max_s
        self.active = False
        self.active_since = 0.0
        self.candidate_since: float | None = None
        self.last_true = 0.0
        self.peak = 0.0
        self.peak_ts = 0.0
        self.peak_frame: np.ndarray | None = None
        self.peak_box: list[float] | None = None
        self.started_at: float | None = None       # candidate_since when it became a moment

    def update(self, on: bool, ts: float, value: float, frame: np.ndarray,
               box: list[float] | None = None) -> str | None:
        if on:
            if not self.active and (self.candidate_since is None or ts - self.last_true > GAP_TOLERANCE):
                self.candidate_since = ts
            self.last_true = ts
            if value > self.peak:
                self.peak, self.peak_ts = value, ts
                self.peak_frame = _thumb(frame)
                if box:
                    self.peak_box = box
        if not self.active:
            if on and ts - self.candidate_since >= self.start_s:
                self.active = True
                self.active_since = ts
                self.started_at = self.candidate_since
                return "start"
            if not on and self.candidate_since is not None and ts - self.last_true > GAP_TOLERANCE:
                self.reset()
            return None
        if not on and ts - self.last_true >= self.end_s:
            self.active = False
            return "end"
        if ts - self.active_since >= self.max_s:
            # Long stretch: close this moment; the next one starts right away if it carries on.
            self.active = False
            return "end"
        return None

    def reset(self) -> None:
        self.active = False
        self.candidate_since = None
        self.peak = 0.0
        self.peak_frame = None
        self.peak_box = None
        self.started_at = None


# Every moment keeps this much more before the action and after it than the per kind settings say,
# so you see what led up to it and how it ended.
EXTRA_ROLL = 5.0

# Which kinds of moment are kept. Bursts (the whole tank suddenly busier) and darts (one fish
# shooting across a quiet tank) are mostly noise in a busy community tank, so they're off unless
# switched on in Setup.
DEFAULT_KINDS = {"spot": True, "feeding": True, "chase": True, "dart": False, "burst": False}


def union_box(boxes, within=None) -> list[float] | None:
    """Smallest box around the given (x, y, w, h) boxes, optionally only those overlapping `within`
    areas (dicts with x, y, w, h)."""
    sel = []
    for b in boxes:
        x, y, w, h = b[:4]
        if within is not None and not any(x < a["x"] + a["w"] and x + w > a["x"] and y < a["y"] + a["h"] and y + h > a["y"]
                                          for a in within):
            continue
        sel.append((x, y, x + w, y + h))
    if not sel:
        return None
    x0, y0 = min(s[0] for s in sel), min(s[1] for s in sel)
    x1, y1 = max(s[2] for s in sel), max(s[3] for s in sel)
    pad = 0.02
    x0, y0, x1, y1 = max(0.0, x0 - pad), max(0.0, y0 - pad), min(1.0, x1 + pad), min(1.0, y1 + pad)
    return [round(x0, 4), round(y0, 4), round(x1 - x0, 4), round(y1 - y0, 4)]


def busiest_box(boxes, top_only: bool = False, grid: int = 4) -> list[float] | None:
    """For whole tank moments (feeding, bursts) in a busy tank, everything moves somewhere, so a
    box around all of it is useless. Find the patch where most is moving: the busiest cell of a
    grid plus touching cells nearly as busy, and box the movement there."""
    if top_only:
        top = [b for b in boxes if b[1] + b[3] / 2 < 0.4]
        boxes = top or boxes
    if not boxes:
        return None
    load = np.zeros((grid, grid))
    cell = lambda b: (min(grid - 1, int((b[1] + b[3] / 2) * grid)), min(grid - 1, int((b[0] + b[2] / 2) * grid)))
    for b in boxes:
        load[cell(b)] += b[2] * b[3]
    r, c = np.unravel_index(int(load.argmax()), load.shape)
    keep = {(r, c)} | {(r + dr, c + dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1)
                       if 0 <= r + dr < grid and 0 <= c + dc < grid and load[r + dr, c + dc] >= 0.5 * load[r, c]}
    return union_box([b for b in boxes if cell(b) in keep])


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
    """Events for whichever tank the camera is looking at. Reset whenever that changes."""

    def __init__(self, zone_cfg: ZoneEventConfig, burst_cfg: BurstEventConfig, feed_cfg: FeedingConfig,
                 db: DB, thumbs_dir: Path, chase_cfg: ChaseEventConfig | None = None):
        self.zcfg, self.bcfg, self.fcfg = zone_cfg, burst_cfg, feed_cfg
        self.ccfg = chase_cfg or ChaseEventConfig()
        self.db = db
        self.thumbs_dir = thumbs_dir
        thumbs_dir.mkdir(parents=True, exist_ok=True)
        self.listeners: list = []      # fn(kind, tank_id, spot_id, ts) when an event starts
        self.closed_listeners: list = []   # fn(event_id) when an event ends
        self.tank = None
        self.open_ids: dict[str, int] = {}
        self.kinds = dict(DEFAULT_KINDS)      # set by the app from the Setup choices
        self.set_tank(None, time.time())

    def set_tank(self, tank, ts: float) -> None:
        """New tank (or settings changed): close anything open and start fresh."""
        self.close_all(ts)
        self.tank = tank
        start_s = tank.zone_start_seconds if tank else self.zcfg.start_seconds
        self.spot_trackers: dict[int, Hysteresis] = {
            s.id: Hysteresis(start_s, self.zcfg.end_seconds, self.zcfg.max_seconds)
            for s in (tank.spotlights if tank else [])}
        self.burst = Hysteresis(self.bcfg.start_seconds, self.bcfg.end_seconds)
        self.feeding = Hysteresis(self.fcfg.start_seconds, self.fcfg.end_seconds)
        self.baseline = Baseline(self.bcfg.baseline_minutes)
        self.last_feeding = float("-inf")
        self._areas = {s.id: s.areas for s in (tank.spotlights if tank else [])}
        c = self.ccfg
        self.chases = ChaseWatch(ChaseConfig(speed=c.speed, min_seconds=c.min_seconds, min_path=c.min_path,
                                             near=c.near, quiet_tank=c.quiet_tank, cooldown=c.cooldown))
        self.chase_open_until = 0.0
        self.tracker = Tracker()
        self.night_window: tuple[str, str] | None = None     # set by the app: (night_start, night_end)

    def update(self, scores: Scores, ts: float, frame: np.ndarray, paused: bool = False) -> None:
        """paused: maintenance mode; keep the baseline learning but make no events."""
        tank = self.tank
        if tank is None:
            return
        quiet = scores.settling or paused
        threshold = tank.zone_threshold
        tracks = self.tracker.update(scores.blobs, ts) if not quiet else []
        fast = [tr.pos for tr in tracks if tr.path_speed() >= PASSER_SPEED]
        slow = [tr.pos for tr in tracks if tr.path_speed() < PASSER_SPEED and tr.pts[-1][0] - tr.pts[0][0] >= SLOW_AGE]
        for spot_id, tr in self.spot_trackers.items():
            value = scores.spots.get(spot_id, 0.0)
            on = value >= threshold and not quiet
            if on and not self._slow_inside(spot_id, slow, fast):
                on = False           # only fish passing through
            box = union_box(scores.boxes, self._areas.get(spot_id)) if on else None
            change = tr.update(on, ts, value, frame, box)
            if self.kinds.get("spot", True):
                self._handle(f"spot:{spot_id}", "spot", spot_id, tr, change, self.zcfg.pre_roll, self.zcfg.post_roll)

        base = self.baseline.update(ts, scores.tank) if not quiet else self.baseline.value
        busy = scores.tank > max(base * tank.burst_ratio, base + self.bcfg.floor) and not quiet

        # Feeding: the tank goes busy *and* most of the movement is up at the surface.
        if tank.setting("feeding_detection"):
            feeding_on = (busy and scores.top_share >= self.fcfg.top_share
                          and (self.feeding.active or ts - self.last_feeding > self.fcfg.cooldown_minutes * 60))
            change = self.feeding.update(feeding_on, ts, scores.tank, frame,
                                         busiest_box(scores.boxes, top_only=True) if feeding_on else None)
            if not self.kinds.get("feeding", True):
                change = None
            if change == "start":
                self.last_feeding = ts
                self._start_feeding(ts)
            elif change == "end":
                self._finish("feeding", self.feeding, self.fcfg.post_roll)

        if self.bcfg.enabled:
            # While feeding, the burst it causes is part of the feeding event.
            on = busy and not self.feeding.active
            change = self.burst.update(on, ts, scores.tank, frame, busiest_box(scores.boxes) if on else None)
            if self.kinds.get("burst", False) or "burst" in self.open_ids:
                self._handle("burst", "burst", None, self.burst, change, self.bcfg.pre_roll, self.bcfg.post_roll)
            elif change == "end":
                self.burst.reset()

        # Chases and darts: at night, a quiet tank (fish asleep) and one thing suddenly tearing across it.
        if self.ccfg.enabled:
            calm = not quiet and not self.feeding.active and not self.burst.active and self._is_night(ts)
            if not calm:
                self.chases.reset()
            found = self.chases.update(scores.blobs, ts, base, quiet_ok=calm)
            if found:
                kind, track, speed = found
                px, py = track.pos
                xs = [p[1] for p in track.pts] + [px]
                ys = [p[2] for p in track.pts] + [py]
                box = [max(0.0, min(xs) - 0.05), max(0.0, min(ys) - 0.05), 0, 0]
                box[2] = min(1.0, max(xs) + 0.05) - box[0]
                box[3] = min(1.0, max(ys) + 0.05) - box[1]
                if self.kinds.get(kind, kind == "chase") or "chase" in self.open_ids:
                    self._start_chase(kind, ts, frame, [round(v, 4) for v in box],
                                      track.pts[0][0] if track.pts else ts)
            if "chase" in self.open_ids and ts >= self.chase_open_until:
                self._close_chase(ts)

    def _slow_inside(self, spot_id: int, slow: list, fast: list) -> bool:
        """Is any slow moving thing inside this animal's areas? (Fast ones are passers by.)"""
        spot = self.tank.spotlight(spot_id)
        areas = self._areas.get(spot_id, []) if hasattr(self, "_areas") else (spot.areas if spot else [])
        def inside(p):
            return any(a["x"] <= p[0] <= a["x"] + a["w"] and a["y"] <= p[1] <= a["y"] + a["h"] for a in areas)
        if any(inside(p) for p in slow):
            return True
        if any(inside(p) for p in fast):
            return False
        # Nothing tracked inside yet (a brand new blob): give it the benefit of the doubt for a moment;
        # the start delay on moments means a passer still has to stay slow for seconds to count.
        return True

    def set_areas(self, areas: dict[int, list[dict]]) -> None:
        """Where each spotlighted animal's areas are in the current picture (after camera adjustment)."""
        self._areas = areas

    def _is_night(self, ts: float) -> bool:
        if not self.night_window:
            return True
        from datetime import datetime
        t = datetime.fromtimestamp(ts).time()
        ns = datetime.strptime(self.night_window[0], "%H:%M").time()
        ne = datetime.strptime(self.night_window[1], "%H:%M").time()
        return (t >= ns or t < ne) if ns > ne else (ns <= t < ne)

    def _start_chase(self, detail: str, ts: float, frame: np.ndarray, box: list[float] | None = None,
                     began: float | None = None) -> None:
        c = self.ccfg
        if "chase" in self.open_ids:
            # Another dart while the clip is still open: extend it, and a chase outranks a dart.
            self.chase_open_until = ts + c.post_roll + EXTRA_ROLL
            self.db.execute("UPDATE events SET action_end_ts=? WHERE id=?", (ts, self.open_ids["chase"]))
            if detail == "chase":
                self.db.execute("UPDATE events SET detail='chase' WHERE id=?", (self.open_ids["chase"],))
            return
        began = began or ts
        ev_id = self.db.open_event(self.tank.id, "chase", None, began - c.pre_roll - EXTRA_ROLL)
        self.db.execute("UPDATE events SET detail=?, peak=?, peak_ts=?, box=?, trigger_ts=?, action_end_ts=? WHERE id=?",
                        (detail, 1.0, ts, json.dumps(box) if box else None, began, ts, ev_id))
        thumb = str(self.thumbs_dir / f"{ev_id}.jpg")
        cv2.imwrite(thumb, _thumb(frame), [cv2.IMWRITE_JPEG_QUALITY, 80])
        self.db.execute("UPDATE events SET thumb=? WHERE id=?", (thumb, ev_id))
        self.open_ids["chase"] = ev_id
        self.chase_open_until = ts + c.post_roll + EXTRA_ROLL
        log.info("%s spotted", detail)
        self._notify_start("chase", None, ts)

    def _close_chase(self, ts: float) -> None:
        ev_id = self.open_ids.pop("chase", None)
        if ev_id is None:
            return
        self.db.execute("UPDATE events SET end_ts=? WHERE id=?", (ts, ev_id))
        for fn in self.closed_listeners:
            fn(ev_id)

    def _start_feeding(self, ts: float) -> None:
        tank_id = self.tank.id
        if "burst" in self.open_ids:
            # The burst that started a moment ago was the feeding rush: relabel it rather than keep both.
            ev_id = self.open_ids.pop("burst")
            self.db.execute("UPDATE events SET kind='feeding' WHERE id=?", (ev_id,))
            self.burst.reset()
        else:
            ev_id = self.db.open_event(tank_id, "feeding", None,
                                       self.feeding.candidate_since - self.fcfg.pre_roll - EXTRA_ROLL)
        self.db.execute("UPDATE events SET trigger_ts=? WHERE id=?", (self.feeding.candidate_since, ev_id))
        self.open_ids["feeding"] = ev_id
        # A "Fed them" tap close by means it's already logged.
        near = self.db.one("SELECT id FROM feedings WHERE tank_id=? AND ts BETWEEN ? AND ?",
                           (tank_id, ts - 900, ts + 60))
        if near:
            self.db.execute("UPDATE feedings SET event_id=? WHERE id=?", (ev_id, near["id"]))
        else:
            self.db.execute("INSERT INTO feedings(tank_id, ts, source, event_id) VALUES (?,?, 'auto', ?)",
                            (tank_id, ts, ev_id))
        log.info("feeding detected")
        self._notify_start("feeding", None, ts)

    def _handle(self, key: str, kind: str, spot_id: int | None, tr: Hysteresis, change: str | None,
                pre: float, post: float) -> None:
        if change == "start":
            self.open_ids[key] = self.db.open_event(self.tank.id, kind, spot_id, tr.candidate_since - pre - EXTRA_ROLL)
            self.db.execute("UPDATE events SET trigger_ts=? WHERE id=?", (tr.candidate_since, self.open_ids[key]))
            log.info("event start: %s", key)
            self._notify_start(kind, spot_id, tr.candidate_since)
        elif change == "end":
            self._finish(key, tr, post)

    def _notify_start(self, kind: str, spot_id: int | None, ts: float) -> None:
        for fn in self.listeners:
            try:
                fn(kind, self.tank.id, spot_id, ts)
            except Exception:
                log.exception("event listener failed")

    def _finish(self, key: str, tr: Hysteresis, post: float) -> None:
        ev_id = self.open_ids.pop(key, None)
        if ev_id is not None:
            thumb = None
            if tr.peak_frame is not None:
                thumb = str(self.thumbs_dir / f"{ev_id}.jpg")
                cv2.imwrite(thumb, tr.peak_frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            self.db.close_event(ev_id, tr.last_true + post + EXTRA_ROLL, tr.peak, tr.peak_ts, thumb)
            self.db.execute("UPDATE events SET box=?, action_end_ts=? WHERE id=?",
                            (json.dumps(tr.peak_box) if tr.peak_box else None, tr.last_true, ev_id))
            log.info("event end: %s (peak %.3f)", key, tr.peak)
            for fn in self.closed_listeners:
                fn(ev_id)
        tr.reset()

    def close_all(self, ts: float) -> None:
        for key, ev_id in list(self.open_ids.items()):
            self.db.execute("UPDATE events SET end_ts=? WHERE id=?", (ts, ev_id))
            for fn in self.closed_listeners:
                fn(ev_id)
        self.open_ids.clear()
