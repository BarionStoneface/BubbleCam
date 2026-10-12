"""Watching for genuinely dangerous things in the tank.

The app reports and never advises, except here: when it sees something dangerous it says what it saw and,
in one line, what that can mean. Never what to do.

  filter bubbles stopped  a "danger": alerts even when notifications are set to quiet
  surface crowding        a "note" until tuned on real footage: status dot and morning summary only
  daytime stillness       a "note" likewise

Each watch only counts time when the camera can actually see properly: not during maintenance, not while
the lights are switching or the camera is moving, not when the picture is frozen or black (those are
separate alerts), and only for the tank the camera is pointed at.
"""

from __future__ import annotations

import collections
import logging
import time
from datetime import datetime, timedelta

import numpy as np

from .config import DangerConfig

log = logging.getLogger(__name__)

BUBBLE_FLOOR = 0.003      # below this share of the box changing, there's effectively nothing moving
BUBBLE_LEARN_S = 120      # seconds of running bubbles before it knows what "normal" looks like
BUBBLE_WINDOW_S = 30      # judge on the middle value of the last 30 seconds, so a fish swimming
                          # through the bubble box is a blip, not "bubbles are back"
SURFACE_GAP_S = 120       # a break shorter than this doesn't reset the surface crowding clock
SURFACE_CLEAR_S = 600
AFTER_FEEDING_S = 1200


BRIGHTNESS_BUCKET = 40    # "normal" is learned separately for each lighting level (light on, IR only...)


class BubbleWatch:
    """Learns how busy a bubble stream normally looks under each lighting level, and notices when it stops.
    It only raises the alarm under lighting it has already seen the bubbles running in."""

    def __init__(self, ratio: float, stop_seconds: float):
        self.ratio, self.stop_seconds = ratio, stop_seconds
        self.normals: dict[int, collections.deque[float]] = {}
        self.bucket = 0
        self.recent: collections.deque[float] = collections.deque(maxlen=BUBBLE_WINDOW_S)
        self.acc: list[float] = []
        self.second: int | None = None
        self.low_since: float | None = None
        self.state = "learning"     # learning, running, stopped

    def update(self, ts: float, score: float | None, usable: bool, brightness: float = 100.0) -> str | None:
        """Feed every analysed frame. Returns 'stopped' or 'running' when the state changes."""
        if not usable or score is None:
            # Can't see properly: don't count this time either way.
            self.acc.clear()
            self.recent.clear()
            self.low_since = None
            return None
        bucket = int(brightness // BRIGHTNESS_BUCKET)
        if bucket != self.bucket:
            self.bucket = bucket
            self.recent.clear()
            self.low_since = None
        sec = int(ts)
        if self.second is None:
            self.second = sec
        if sec == self.second:
            self.acc.append(score)
            return None
        value = float(np.mean(self.acc)) if self.acc else score
        self.acc = [score]
        self.second = sec
        return self._second(ts, value)

    def _second(self, ts: float, value: float) -> str | None:
        normal_q = self.normals.setdefault(self.bucket, collections.deque(maxlen=1800))
        if len(normal_q) < BUBBLE_LEARN_S:
            if value >= BUBBLE_FLOOR:
                normal_q.append(value)
            return None
        self.recent.append(value)
        if len(self.recent) < BUBBLE_WINDOW_S // 2:
            return None
        normal = float(np.median(normal_q))
        low = float(np.median(self.recent)) < max(normal * self.ratio, BUBBLE_FLOOR)
        if low:
            if self.low_since is None:
                self.low_since = ts
            if self.state != "stopped" and ts - self.low_since >= self.stop_seconds:
                self.state = "stopped"
                return "stopped"
            return None
        self.low_since = None
        normal_q.append(value)
        if self.state != "running":
            was = self.state
            self.state = "running"
            return "running" if was == "stopped" else None
        return None


class SurfaceWatch:
    """Most of the tank's movement up at the surface for a long stretch, not explained by feeding."""

    def __init__(self, share: float, minutes: float):
        self.share, self.seconds = share, minutes * 60
        self.since: float | None = None
        self.last_crowded = 0.0
        self.active = False

    def update(self, ts: float, top_share: float, tank: float, usable: bool, fed_recently: bool) -> str | None:
        if not usable:
            return None
        crowded = top_share >= self.share and tank >= 0.01 and not fed_recently
        if crowded:
            if self.since is None or ts - self.last_crowded > SURFACE_GAP_S:
                self.since = ts
            self.last_crowded = ts
            if not self.active and ts - self.since >= self.seconds:
                self.active = True
                return "start"
        elif self.active and ts - self.last_crowded > SURFACE_CLEAR_S:
            self.active = False
            self.since = None
            return "end"
        return None


class Danger:
    def __init__(self, core, cfg: DangerConfig):
        self.core = core
        self.cfg = cfg
        self.bubbles: dict[int, BubbleWatch] = {}
        self.surface: dict[int, SurfaceWatch] = {}
        self.unwatched_since: dict[int, float] = {}
        self.last_periodic = 0.0

    # Per analysed frame, for the tank the camera is on

    def usable(self, now: float, scores) -> bool:
        c = self.core
        return (not scores.settling and not c.view.state.unsure and scores.brightness >= 6
                and now - c.last_change_wall < 10 and not c._in_maintenance(now))

    def update(self, tank, ts: float, scores) -> None:
        if tank is None:
            return
        now = time.time()
        usable = self.usable(now, scores)
        if tank.setting("bubbles") and tank.setting("bubble_alert"):
            bw = self.bubbles.get(tank.id)
            if bw is None:
                bw = self.bubbles[tank.id] = BubbleWatch(self.cfg.bubble_ratio, self.cfg.bubble_stop_seconds)
            change = bw.update(ts, scores.bubbles, usable, scores.brightness)
            if change == "stopped":
                self.core.health.set_external(
                    f"bubbles:{tank.id}", True, f"The filter bubbles in {tank.name} have stopped",
                    "danger", since=bw.low_since)
            elif change == "running":
                self.core.health.set_external(f"bubbles:{tank.id}", False)

        sw = self.surface.get(tank.id)
        if sw is None:
            sw = self.surface[tank.id] = SurfaceWatch(self.cfg.surface_share, self.cfg.surface_minutes)
        fed = ts - self.core.events.last_feeding < AFTER_FEEDING_S
        change = sw.update(ts, scores.top_share, scores.tank, usable, fed)
        if change == "start":
            self.core.health.set_external(
                f"surface:{tank.id}", True,
                f"Most of the movement in {tank.name} has been at the surface for "
                f"{self.cfg.surface_minutes:.0f} minutes. That can mean low oxygen.", "note", since=sw.since)
        elif change == "end":
            self.core.health.set_external(f"surface:{tank.id}", False)

    # Every few seconds, from the health loop

    def periodic(self, now: float) -> None:
        c = self.core
        for tank in c.tanks.all():
            watching = tank.setting("bubbles") and tank.setting("bubble_alert")
            key = f"bubbles:{tank.id}"
            if not watching:
                self.bubbles.pop(tank.id, None)
                c.health.set_external(key, False)
            # Someone relies on the bubble watch: say so when it can't see them.
            in_view = tank.id == c.tank_id and not c.view.state.unsure
            ukey = f"unwatched:{tank.id}"
            if watching and not in_view:
                self.unwatched_since.setdefault(tank.id, now)
                if now - self.unwatched_since[tank.id] >= 60:
                    c.health.set_external(ukey, True,
                                          f"Not watching the filter bubbles in {tank.name}: the camera is pointed elsewhere",
                                          "note", since=self.unwatched_since[tank.id])
            else:
                self.unwatched_since.pop(tank.id, None)
                c.health.set_external(ukey, False)
        if now - self.last_periodic >= 300:
            self.last_periodic = now
            self._stillness(now)

    def _stillness(self, now: float) -> None:
        c = self.core
        tank = c.tank
        if tank is None:
            return
        key = f"still:{tank.id}"
        dt = datetime.fromtimestamp(now)
        day_start = datetime.strptime(self.cfg.day_start, "%H:%M").time()
        day_end = datetime.strptime(self.cfg.day_end, "%H:%M").time()
        if not (day_start <= dt.time() <= day_end):
            c.health.set_external(key, False)
            return
        hour_ago = now - 3600
        watched = sum(b - a for a, b in c.db.tank_intervals(tank.id, hour_ago, now))
        if watched < 3300:
            return
        current = c.db.value("""SELECT AVG(tank_mean) FROM activity WHERE tank_id=? AND maintenance=0
                                AND minute_ts >= ?""", (tank.id, hour_ago))
        typical = []
        for back in range(1, 8):
            a = (dt - timedelta(days=back)).timestamp()
            v = c.db.value("""SELECT AVG(tank_mean) FROM activity WHERE tank_id=? AND maintenance=0
                              AND minute_ts >= ? AND minute_ts < ?""", (tank.id, a - 3600, a))
            if v is not None:
                typical.append(v)
        if current is None or len(typical) < 3:
            return
        usual = float(np.median(typical))
        if usual < 0.002:
            return
        if current < usual * self.cfg.still_ratio:
            pct = max(1, round(100 * current / usual))
            c.health.set_external(key, True,
                                  f"{tank.name} has been much stiller than usual for the last hour "
                                  f"(about {pct}% of its normal daytime movement). That can mean a heater or "
                                  f"oxygen problem.", "note", since=hour_ago)
        elif current > usual * self.cfg.still_ratio * 2:
            c.health.set_external(key, False)

    def bubble_status(self, tank) -> str:
        if not tank.setting("bubbles"):
            return "no box"
        if not tank.setting("bubble_alert"):
            return "off"
        if tank.id != self.core.tank_id:
            return "not in view"
        bw = self.bubbles.get(tank.id)
        return bw.state if bw else "learning"
