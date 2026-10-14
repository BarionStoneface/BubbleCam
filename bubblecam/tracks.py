"""Short tracks of moving things, and what they tell us.

One camera flattens the tank and fish cross in front of each other constantly, so this never tries to
follow a fish for long. It follows a moving blob for a second or two, which is enough to know how fast
it's going and whether another blob is right behind it. Three things are built on that:

  ChaseWatch   a fast dart across the tank while the tank is otherwise quiet (the night time chase
               that may be one fish bullying another); a second blob close behind makes it a "chase",
               a lone one a "dart"
  Behaviour    for a spotlighted animal: is it travelling, or busy without going anywhere (a shrimp
               fanning, a pleco grazing)?
  distance     how far a spotlighted animal travelled, for the nightly numbers

All positions are fractions of the frame; speeds are frame widths per second.
"""

from __future__ import annotations

import collections
import math
from dataclasses import dataclass, field


@dataclass
class Track:
    id: int
    pts: collections.deque          # (ts, cx, cy)
    area: float
    last_ts: float
    hits: int = 1
    path: float = 0.0               # total distance moved
    fast_since: float | None = None

    @property
    def pos(self) -> tuple[float, float]:
        return self.pts[-1][1], self.pts[-1][2]

    def speed(self, window: float = 0.3) -> float:
        """Average speed over the last `window` seconds."""
        if len(self.pts) < 2:
            return 0.0
        t1, x1, y1 = self.pts[-1]
        for t0, x0, y0 in reversed(self.pts):
            if t1 - t0 >= window:
                break
        dt = t1 - t0
        return math.hypot(x1 - x0, y1 - y0) / dt if dt > 0 else 0.0

    def path_speed(self, step: float = 0.2) -> float:
        """Distance travelled along the path over the whole history, per second, sampled every `step`
        seconds so jitter doesn't count. A fish that swims in and turns around still travels far."""
        if len(self.pts) < 2:
            return 0.0
        total, last = 0.0, self.pts[0]
        for p in self.pts:
            if p[0] - last[0] >= step:
                total += math.hypot(p[1] - last[1], p[2] - last[2])
                last = p
        dur = self.pts[-1][0] - self.pts[0][0]
        return total / dur if dur > 0.3 else 0.0

    def heading(self, window: float = 0.3) -> tuple[float, float]:
        if len(self.pts) < 2:
            return (0.0, 0.0)
        t1, x1, y1 = self.pts[-1]
        for t0, x0, y0 in reversed(self.pts):
            if t1 - t0 >= window:
                break
        dx, dy = x1 - x0, y1 - y0
        n = math.hypot(dx, dy)
        return (dx / n, dy / n) if n > 1e-6 else (0.0, 0.0)


class Tracker:
    """Greedy nearest neighbour matching, which is plenty for blobs a few frames apart."""

    def __init__(self, max_speed: float = 4.0, lost_after: float = 0.4, history: float = 2.0):
        self.max_speed = max_speed       # frame widths per second a blob can plausibly move
        self.lost_after = lost_after
        self.history = history
        self.tracks: dict[int, Track] = {}
        self.next_id = 1
        self.last_ts: float | None = None

    def update(self, blobs: list[tuple[float, float, float, float, float]], ts: float) -> list[Track]:
        dt = (ts - self.last_ts) if self.last_ts is not None else 1 / 30
        self.last_ts = ts
        reach = max(0.02, self.max_speed * max(dt, 1e-3))
        pairs = []
        for i, (cx, cy, w, h, area) in enumerate(blobs):
            for tid, tr in self.tracks.items():
                px, py = tr.pos
                d = math.hypot(cx - px, cy - py)
                if d <= reach:
                    pairs.append((d, i, tid))
        pairs.sort()
        used_b, used_t = set(), set()
        for d, i, tid in pairs:
            if i in used_b or tid in used_t:
                continue
            used_b.add(i)
            used_t.add(tid)
            tr = self.tracks[tid]
            cx, cy, w, h, area = blobs[i]
            px, py = tr.pos
            tr.path += math.hypot(cx - px, cy - py)
            tr.pts.append((ts, cx, cy))
            tr.area = 0.7 * tr.area + 0.3 * area
            tr.last_ts = ts
            tr.hits += 1
            while tr.pts and ts - tr.pts[0][0] > self.history:
                tr.pts.popleft()
        for i, (cx, cy, w, h, area) in enumerate(blobs):
            if i not in used_b:
                self.tracks[self.next_id] = Track(self.next_id, collections.deque([(ts, cx, cy)]), area, ts)
                self.next_id += 1
        for tid in [t for t, tr in self.tracks.items() if ts - tr.last_ts > self.lost_after]:
            del self.tracks[tid]
        return [tr for tr in self.tracks.values() if tr.last_ts == ts]

    def reset(self) -> None:
        self.tracks.clear()
        self.last_ts = None


@dataclass
class ChaseConfig:
    speed: float = 0.5            # frame widths per second that counts as a dart
    min_seconds: float = 0.3      # ...sustained this long
    min_path: float = 0.15        # ...covering at least this much of the frame
    near: float = 0.09            # a second blob within this distance, going the same way, makes it a chase
    quiet_tank: float = 0.006     # only when whole tank movement is below this (fish asleep)
    cooldown: float = 20.0        # darts closer together than this are one event


class ChaseWatch:
    """Finds darts and chases while the tank is quiet. Returns ("chase" | "dart", track, speed) when
    one starts, nothing otherwise. Timing (pre roll, clip length) is the event manager's job."""

    def __init__(self, cfg: ChaseConfig | None = None):
        self.cfg = cfg or ChaseConfig()
        self.tracker = Tracker()
        self.last_event = float("-inf")

    def reset(self) -> None:
        self.tracker.reset()

    def update(self, blobs, ts: float, tank_motion: float, quiet_ok: bool) -> tuple[str, Track, float] | None:
        tracks = self.tracker.update(blobs, ts)
        cfg = self.cfg
        if not quiet_ok or tank_motion > cfg.quiet_tank or ts - self.last_event < cfg.cooldown:
            for tr in tracks:
                tr.fast_since = None
            return None
        best = None
        for tr in tracks:
            sp = tr.speed()
            if sp >= cfg.speed:
                tr.fast_since = tr.fast_since or ts
                if ts - tr.fast_since >= cfg.min_seconds and tr.path >= cfg.min_path:
                    if best is None or sp > best[1]:
                        best = (tr, sp)
            else:
                tr.fast_since = None
        if best is None:
            return None
        tr, sp = best
        hx, hy = tr.heading()
        kind = "dart"
        for other in tracks:
            if other is tr:
                continue
            ox, oy = other.pos
            px, py = tr.pos
            if math.hypot(ox - px, oy - py) <= cfg.near and other.speed() >= cfg.speed * 0.5:
                ohx, ohy = other.heading()
                if hx * ohx + hy * ohy > 0.3:
                    kind = "chase"
                    break
        self.last_event = ts
        return kind, tr, sp


class Behaviour:
    """For one spotlighted animal: moving around, or busy in one place?

    Looks at where its movement is centred over the last `window` seconds. If that centre wanders,
    it's travelling; if it stays put while the animal keeps moving, it's working one spot."""

    def __init__(self, window: float = 10.0, travel: float = 0.025):
        self.window = window
        self.travel = travel          # fraction of the frame the centre has to wander to count as travelling
        self.pts: collections.deque = collections.deque()
        self.distance = 0.0           # total travel, in frame widths
        self.last: tuple[float, float] | None = None
        self.last_ts: float | None = None

    def update(self, ts: float, active: bool, centroid: tuple[float, float] | None) -> str:
        """Returns 'quiet', 'still' (busy in one place) or 'moving'."""
        if not active or centroid is None:
            self.pts.clear()
            self.last = None
            return "quiet"
        cx, cy = centroid
        if self.last is not None and self.last_ts is not None and ts - self.last_ts < 2.0:
            step = math.hypot(cx - self.last[0], cy - self.last[1])
            # Steps bigger than a body length in one frame are the centroid jumping between parts of
            # the animal (tail flick, fans), not travel.
            if step < 0.05:
                self.distance += step
        self.last, self.last_ts = (cx, cy), ts
        self.pts.append((ts, cx, cy))
        while self.pts and ts - self.pts[0][0] > self.window:
            self.pts.popleft()
        if ts - self.pts[0][0] < self.window * 0.5:
            return "still"
        pts = list(self.pts)
        half = len(pts) // 2
        first, second = pts[:half], pts[half:]
        ax, ay = _median([p[1] for p in first]), _median([p[2] for p in first])
        bx, by = _median([p[1] for p in second]), _median([p[2] for p in second])
        return "moving" if math.hypot(bx - ax, by - ay) >= self.travel else "still"


def _median(values: list[float]) -> float:
    v = sorted(values)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2
