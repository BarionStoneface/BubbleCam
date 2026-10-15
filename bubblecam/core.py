"""Wires camera, view tracking, detection, events, recording, storage and health together."""

from __future__ import annotations

import collections
import logging
import threading
import time
from datetime import datetime

import cv2
import numpy as np

from .camera import open_source
from .config import Config
from .danger import Danger
from .db import DB
from .detector import Detector, Scores
from .events import EventManager
from .health import Health
from .maps import HeatMaps, TrailsWorker
from .notify import Notifier
from .presence import PresenceManager
from .recorder import Recorder
from .storage import Storage
from .summary import incidents, morning_text, night_bounds, tank_summary
from .tanks import IDENTITY, MAX_SPOTLIGHTS, TankStore, transform_rect
from .timelapse import TimelapseWorker
from .tracks import Behaviour
from .viewmatch import ViewTracker

log = logging.getLogger(__name__)

FIRST_TANK_AFTER = 5.0   # seconds of pictures before a brand new install sets up "Tank 1" by itself


class MinuteStats:
    """Per minute activity for the tank being watched (and each of its spotlighted animals)."""

    def __init__(self, db: DB):
        self.db = db
        self.key: tuple[int, int] | None = None   # (tank id, minute)
        self.tank: list[float] = []
        self.spot_moving: dict[int, float] = {}
        self.spot_still: dict[int, float] = {}
        self.spot_max: dict[int, float] = {}
        self.behaviour: dict[int, Behaviour] = {}
        self.modes: dict[int, str] = {}          # 'quiet' / 'still' / 'moving' per animal, steadied over 10 s
        self._mode_hist: dict[int, collections.deque] = {}
        self.maintenance = False
        self.last_ts: float | None = None

    def add(self, tank_id: int | None, ts: float, scores: Scores, threshold: float, maintenance: bool) -> None:
        if tank_id is None:
            self.flush()
            self.key = None
            return
        key = (tank_id, int(ts // 60) * 60)
        if self.key is not None and key != self.key:
            self.flush()
        self.key = key
        dt = min(ts - self.last_ts, 1.0) if self.last_ts else 0.0
        self.last_ts = ts
        self.tank.append(scores.tank)
        self.maintenance |= maintenance
        for spot_id, v in scores.spots.items():
            active = v >= threshold
            b = self.behaviour.get(spot_id)
            if b is None:
                b = self.behaviour[spot_id] = Behaviour()
            mode = b.update(ts, active and not maintenance, scores.centroids.get(spot_id))
            hist = self._mode_hist.setdefault(spot_id, collections.deque())
            hist.append((ts, mode))
            while hist and ts - hist[0][0] > 10:
                hist.popleft()
            counts = collections.Counter(m for _, m in hist)
            busy = counts["still"] + counts["moving"]
            self.modes[spot_id] = ("quiet" if busy < len(hist) * 0.3
                                   else "still" if counts["still"] >= counts["moving"] else "moving")
            if active:
                self.spot_moving[spot_id] = self.spot_moving.get(spot_id, 0.0) + dt
                if mode == "still":
                    self.spot_still[spot_id] = self.spot_still.get(spot_id, 0.0) + dt
            self.spot_max[spot_id] = max(self.spot_max.get(spot_id, 0.0), v)

    def flush(self) -> None:
        if self.key is None or not self.tank:
            return
        tank_id, minute = self.key
        t = np.array(self.tank)
        self.db.execute("INSERT OR REPLACE INTO activity VALUES (?,?,?,?,?)",
                        (tank_id, minute, float(t.mean()), float(t.max()), int(self.maintenance)))
        for spot_id, mx in self.spot_max.items():
            self.db.execute("INSERT OR REPLACE INTO spot_activity VALUES (?,?,?,?,?,?)",
                            (spot_id, minute, tank_id, self.spot_moving.get(spot_id, 0.0), mx,
                             self.spot_still.get(spot_id, 0.0)))
        self.tank.clear()
        self.spot_moving.clear()
        self.spot_still.clear()
        self.spot_max.clear()
        self.maintenance = False


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.data = cfg.data_path
        self.data.mkdir(parents=True, exist_ok=True)
        self.db = DB(self.data / "bubblecam.db")
        # Anything left open by a crash or power cut.
        self.db.execute("UPDATE events SET end_ts = start_ts + 60 WHERE end_ts IS NULL")
        cam = cfg.camera
        self.tanks = TankStore(self.db)
        self.detector = Detector(cfg.detect, cam.width, cam.height)
        self.events = EventManager(cfg.zone_events, cfg.burst_events, cfg.feeding, self.db, self.data / "thumbs",
                                   cfg.chase)
        self.events.night_window = (cfg.timelapse.night_start, cfg.timelapse.night_end)
        self.recorder = (Recorder(cfg.record, self.data / "rec", cam.width, cam.height, cam.fps, self.db)
                         if cfg.record.enabled else None)
        self.storage = Storage(cfg.storage, self.db, self.data,
                               lambda: self.recorder.current_segment_id if self.recorder else None)
        self.timelapses = TimelapseWorker(self.db, cfg, self.tanks)
        self.presence = PresenceManager(cfg.presence, self.db, self.data)
        self.notifier = Notifier(cfg, self.db, self.data, lambda: self.latest, self._morning)
        self.stats = MinuteStats(self.db)

        self.lock = threading.Lock()
        self.latest: np.ndarray | None = None
        self.latest_ts = 0.0
        self.scores = Scores()
        self.fps = 0.0
        self.frame_cond = threading.Condition()
        self.running = True
        self.started = time.time()
        self.last_frame_wall = 0.0
        self.last_change_wall = time.time()
        self._drops: list[tuple[float, int, int]] = []
        self.frames_total = 0
        self.capture_thread: threading.Thread | None = None
        # Timing, as running averages in ms: the whole loop per frame, and analysis alone.
        self.loop_ms = 0.0
        self.analyze_ms = 0.0
        self.stride = max(1, cfg.detect.analyze_every)
        self._slow_since: float | None = None
        self._fast_since: float | None = None

        self.tank_id: int | None = None
        self.A = IDENTITY.copy()
        self.view = ViewTracker(self.db, self.data, self._view_frame, self._view_changed)
        self.tank_id, self.A = self.view.state.tank_id, self.view.state.A
        self.health = Health(self)
        self.danger = Danger(self, cfg.danger)
        self.heat = HeatMaps(self.data, cfg.timelapse.night_start, cfg.timelapse.night_end)
        self.trails = TrailsWorker(self.db, self.tanks, self.data, cfg.timelapse.night_start, cfg.timelapse.night_end)

        self.presence.listeners.append(self._on_presence)
        self.events.listeners.append(self._on_event)
        self.events.closed_listeners.append(self.storage.event_closed)
        self.tanks.listeners.append(lambda: self._apply_layout(reset_events=True))
        self._apply_layout(reset_events=True)

    # The tank being watched

    @property
    def tank(self):
        return self.tanks.get(self.tank_id)

    def _view_frame(self):
        return self.latest, self.scores

    def _view_changed(self, tank_id: int | None, A: np.ndarray, switched: bool) -> None:
        self.tank_id, self.A = tank_id, A
        if switched:
            self.detector.restart(time.time(), 10.0)
        self._apply_layout(reset_events=switched)
        if tank_id is None and self.latest is not None:
            # The camera is looking at something it doesn't know. If the only tank is still a blank one
            # (nobody has drawn a box or named an animal yet), it was probably set up from whatever the
            # camera first saw, so just follow the camera: this view becomes that tank.
            tanks = self.tanks.all()
            if len(tanks) == 1 and not tanks[0].spotlights and not tanks[0].setting("ignore") \
                    and not tanks[0].setting("bubbles"):
                log.info("following the camera: %s is now this view", tanks[0].name)
                threading.Thread(target=self.view.anchor, args=(tanks[0].id, self.latest), daemon=True).start()

    def _apply_layout(self, reset_events: bool) -> None:
        tank, A = self.tank, self.A
        if tank is None:
            self.detector.set_layout({}, [])
        else:
            spots = {s.id: [transform_rect(a, A) for a in s.areas] for s in tank.spotlights}
            ignore = [transform_rect(r, A) for r in tank.setting("ignore")]
            bubbles = tank.setting("bubbles")
            self.detector.set_layout(spots, ignore, transform_rect(bubbles, A) if bubbles else None)
            self.events.set_areas(spots)
        homes = {s.id: transform_rect(s.home, A) for s in tank.spotlights if s.home} if tank else {}
        self.presence.set_layout(tank, homes)
        if reset_events:
            self.events.set_tank(tank, time.time())

    def layout(self) -> dict | None:
        """The current tank's boxes, placed where they are in the picture right now."""
        tank = self.tank
        return tank.to_dict(self.A) if tank else None

    def rebase(self) -> None:
        """Before editing boxes: make 'where things are now' the tank's new reference, so boxes drawn on the
        current picture and existing ones line up."""
        tank, A, frame = self.tank, self.A, self.latest
        if tank is None or frame is None:
            return
        if np.abs(A - IDENTITY).max() > 1e-6:
            moved = [(s.id, [transform_rect(a, A) for a in s.areas], transform_rect(s.home, A) if s.home else None)
                     for s in tank.spotlights]
            ignore = [transform_rect(r, A) for r in tank.setting("ignore")]
            bubbles = transform_rect(tank.setting("bubbles"), A) if tank.setting("bubbles") else None
            # From here on, stored boxes are in today's picture coordinates.
            self.A = IDENTITY.copy()
            for spot_id, areas, home in moved:
                self.presence.stored_home[spot_id] = home
                self.tanks.update_spotlight(spot_id, areas=areas, home=home)
            self.tanks.update_settings(tank.id, {"ignore": ignore, "bubbles": bubbles})
        self.view.anchor(tank.id, frame)

    def new_tank_here(self, name: str):
        """Set up a new tank from what the camera sees right now."""
        if self.latest is None:
            raise ValueError("no picture from the camera yet")
        tank = self.tanks.create(name)
        self.view.anchor(tank.id, self.latest)
        return tank

    def this_is(self, tank_id: int) -> None:
        """The camera is looking at this tank (re-anchor after a big move or a missed recognition)."""
        if self.latest is None:
            raise ValueError("no picture from the camera yet")
        if self.tanks.get(tank_id) is None:
            raise ValueError("no such tank")
        self.view.anchor(tank_id, self.latest)

    def add_spotlight(self, tank_id: int, name: str):
        if len(self.tanks.get(tank_id).spotlights) >= MAX_SPOTLIGHTS:
            raise ValueError(f"a tank can spotlight up to {MAX_SPOTLIGHTS} animals")
        return self.tanks.add_spotlight(tank_id, name)

    # Maintenance

    def maintenance_until(self, tank_id: int | None) -> float | None:
        if tank_id is None:
            return None
        row = self.db.one("SELECT end_ts FROM maintenance WHERE tank_id=? AND end_ts > ? ORDER BY end_ts DESC LIMIT 1",
                          (tank_id, time.time()))
        return row["end_ts"] if row else None

    def start_maintenance(self, tank_id: int, minutes: float) -> float:
        now = time.time()
        end = now + minutes * 60
        current = self.db.one("SELECT id FROM maintenance WHERE tank_id=? AND end_ts > ?", (tank_id, now))
        if current:
            self.db.execute("UPDATE maintenance SET end_ts=? WHERE id=?", (end, current["id"]))
        else:
            self.db.execute("INSERT INTO maintenance(tank_id, start_ts, end_ts) VALUES (?,?,?)", (tank_id, now, end))
        # Hands in the tank can look like a camera move; don't let it re-learn the view meanwhile.
        self.view.pause(minutes * 60)
        self._maint_cache = (0.0, None)
        return end

    def stop_maintenance(self, tank_id: int) -> None:
        now = time.time()
        self.db.execute("UPDATE maintenance SET end_ts=? WHERE tank_id=? AND end_ts > ?", (now, tank_id, now))
        self.view.pause(0)
        self.detector.restart(now, 10.0)
        self._maint_cache = (0.0, None)

    _maint_cache: tuple[float, float | None] = (0.0, None)

    def _in_maintenance(self, now: float) -> bool:
        checked, until = self._maint_cache
        if now - checked > 5:
            until = self.maintenance_until(self.tank_id)
            self._maint_cache = (now, until)
        return until is not None and until > now

    # Notifications

    def _on_presence(self, tank_id: int, spot_id: int, state: str, ts: float) -> None:
        spot = self.tanks.get(tank_id).spotlight(spot_id) if self.tanks.get(tank_id) else None
        if state == "away" and spot:
            self.notifier.animal_out(spot_id, spot.name, ts)

    def _on_event(self, kind: str, tank_id: int, spot_id: int | None, ts: float) -> None:
        tank = self.tanks.get(tank_id)
        if tank is None:
            return
        if kind == "burst":
            self.notifier.burst(tank.name, ts)
        elif kind == "spot":
            spot = tank.spotlight(spot_id)
            # Without home detection, the animal turning up in its areas is the best "it's out" signal.
            if spot and not spot.home:
                self.notifier.animal_out(spot_id, spot.name, ts)

    def _morning(self) -> tuple[str, str, str | None]:
        t = self.cfg.timelapse
        start, end = night_bounds(datetime.now(), t.night_start, t.night_end)
        summaries = [tank_summary(self.db, tank, start, end) for tank in self.tanks.all()]
        summaries.sort(key=lambda s: s["watched_hours"], reverse=True)
        return morning_text(summaries, incidents(self.db, start, end))

    # Main loop

    def start(self) -> None:
        self.capture_thread = threading.Thread(target=self._capture_loop, name="capture", daemon=True)
        self.capture_thread.start()
        self.view.start()
        self.storage.start()
        self.health.start()
        self.trails.start()
        threading.Thread(target=self._tank_pictures, name="tank-pictures", daemon=True).start()

    def _tank_pictures(self) -> None:
        """Keep a recent picture of each tank for the home screen cards: every minute, and right after
        the camera moves to a tank."""
        saved_for, saved_at = None, 0.0
        while self.running:
            time.sleep(5)
            frame, tank_id = self.latest, self.tank_id
            if tank_id == saved_for and time.time() - saved_at < 60:
                continue
            if frame is not None and tank_id is not None:
                saved_for, saved_at = tank_id, time.time()
                try:
                    folder = self.data / "tanks" / str(tank_id)
                    folder.mkdir(parents=True, exist_ok=True)
                    small = cv2.resize(frame, (640, round(frame.shape[0] * 640 / frame.shape[1])),
                                       interpolation=cv2.INTER_AREA)
                    cv2.imwrite(str(folder / "last.tmp.jpg"), small, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    (folder / "last.tmp.jpg").replace(folder / "last.jpg")
                except OSError:
                    log.exception("couldn't save tank picture")

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        self.events.close_all(time.time())
        self.stats.flush()
        self.heat.save()
        if self.recorder:
            self.recorder.stop()

    def capture_alive(self) -> bool:
        return self.capture_thread is not None and self.capture_thread.is_alive()

    def drop_rate(self) -> float:
        """Share of frames the recorder dropped over the last minute."""
        if not self.recorder or len(self._drops) < 2:
            return 0.0
        (t0, d0, f0), (t1, d1, f1) = self._drops[0], self._drops[-1]
        frames = f1 - f0
        return (d1 - d0) / frames if frames > 0 else 0.0

    def _capture_loop(self) -> None:
        n = 0
        fps_t, fps_n = time.time(), 0
        scores = Scores()
        prev_tiny = None
        try:
            for frame, ts in open_source(self.cfg.camera):
                if not self.running:
                    break
                now = time.time()
                loop_t = time.perf_counter()
                self.last_frame_wall = now
                if n % 15 == 0:
                    tiny = cv2.resize(frame, (32, 20), interpolation=cv2.INTER_AREA)
                    if prev_tiny is None or not np.array_equal(tiny, prev_tiny):
                        self.last_change_wall = now
                    prev_tiny = tiny
                if not self.tanks.all() and now - self.started > FIRST_TANK_AFTER:
                    self.latest = frame
                    log.info("first run: setting up Tank 1 from the current view")
                    self.new_tank_here("Tank 1")

                tank = self.tank
                maintenance = self._in_maintenance(now)
                paused = maintenance
                if n % self.stride == 0:
                    t_a = time.perf_counter()
                    scores = self.detector.analyze(frame, ts)
                    self.analyze_ms = 0.95 * self.analyze_ms + 0.05 * (time.perf_counter() - t_a) * 1000
                    self.events.update(scores, ts, frame, paused=paused)
                    self.danger.update(tank, ts, scores)
                    if not paused:
                        self.presence.update(frame, ts, scores.settling)
                        if tank is not None and not scores.settling:
                            self.heat.add(tank.id, ts, scores.mask)
                n += 1
                self.stats.add(self.tank_id, ts, scores, tank.zone_threshold if tank else 1.0, maintenance)
                if self.recorder:
                    spots = np.zeros(4, np.float32)
                    if tank:
                        for s in tank.spotlights:
                            spots[s.slot] = scores.spots.get(s.id, 0.0)
                    self.recorder.submit(frame, ts, scores.tank, spots,
                                         self.tank_id if self.tank_id is not None else -1)
                with self.frame_cond:
                    self.latest, self.latest_ts, self.scores = frame, ts, scores
                    self.frame_cond.notify_all()
                self.frames_total += 1
                fps_n += 1
                self.loop_ms = 0.95 * self.loop_ms + 0.05 * (time.perf_counter() - loop_t) * 1000
                self._adapt_stride(now)
                if ts - fps_t >= 2.0:
                    self.fps = fps_n / (ts - fps_t)
                    fps_t, fps_n = ts, 0
                    if self.recorder:
                        self._drops.append((now, self.recorder.dropped, self.frames_total))
                        self._drops = [d for d in self._drops if now - d[0] <= 60]
        except Exception:
            log.exception("capture loop crashed")
            raise

    def _adapt_stride(self, now: float) -> None:
        """If the Pi can't keep up, analyse every other frame rather than drop frames; go back when it can."""
        budget = 1000.0 / max(1, self.cfg.camera.fps)
        base = max(1, self.cfg.detect.analyze_every)
        if self.loop_ms > budget * 0.8:
            self._fast_since = None
            self._slow_since = self._slow_since or now
            if now - self._slow_since > 10 and self.stride < 4:
                self.stride += 1
                self._slow_since = None
                log.warning("falling behind (%.0f ms per frame); analysing every %d frames", self.loop_ms, self.stride)
        elif self.loop_ms < budget * 0.35 and self.stride > base:
            self._slow_since = None
            self._fast_since = self._fast_since or now
            if now - self._fast_since > 120:
                self.stride -= 1
                self._fast_since = None
                log.info("caught up; analysing every %d frames", self.stride)
        else:
            self._slow_since = None
            self._fast_since = None

    def wait_frame(self, after_ts: float, timeout: float = 2.0) -> tuple[np.ndarray | None, float, Scores]:
        with self.frame_cond:
            self.frame_cond.wait_for(lambda: self.latest_ts > after_ts, timeout=timeout)
            return self.latest, self.latest_ts, self.scores

    def snapshot_jpeg(self, width: int | None = None, overlay: bool = False) -> bytes | None:
        frame, scores = self.latest, self.scores
        if frame is None:
            return None
        return encode_view(frame, scores, self.layout() if overlay else None, width, overlay)


def encode_view(frame: np.ndarray, scores: Scores, layout: dict | None, width: int | None,
                overlay: bool) -> bytes:
    img = frame
    if width and width < frame.shape[1]:
        img = cv2.resize(frame, (width, round(frame.shape[0] * width / frame.shape[1])),
                         interpolation=cv2.INTER_AREA)
    if overlay:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        h, w = img.shape[:2]

        def box(r, color, label=None, thick=2):
            p0 = (int(r["x"] * w), int(r["y"] * h))
            p1 = (int((r["x"] + r["w"]) * w), int((r["y"] + r["h"]) * h))
            cv2.rectangle(img, p0, p1, color, thick)
            if label:
                cv2.putText(img, label, (p0[0] + 4, p0[1] + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1,
                            cv2.LINE_AA)

        if layout:
            for r in layout["settings"]["ignore"]:
                box(r, (90, 90, 90), None, 1)
            if layout["settings"].get("bubbles"):
                box(layout["settings"]["bubbles"], (240, 170, 90), "bubbles")
            for s in layout["spotlights"]:
                for a in s["areas"]:
                    box(a, (192, 208, 95), s["name"])
                if s["home"]:
                    box(s["home"], (90, 180, 240), f"{s['name']} home")
        for bx, by, bw, bh in scores.boxes:
            cv2.rectangle(img, (int(bx * w), int(by * h)), (int((bx + bw) * w), int((by + bh) * h)),
                          (90, 255, 120), 1)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 75])
    return buf.tobytes()
