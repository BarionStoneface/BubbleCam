"""Wires camera, detector, events, recorder and housekeeping together."""

from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from .camera import open_source
from .config import Config, load_zones, save_zones
from .db import DB
from .detector import Detector, Scores
from .events import EventManager
from .recorder import Recorder, segment_files
from .timelapse import TimelapseWorker

log = logging.getLogger(__name__)


class MinuteStats:
    def __init__(self, db: DB, zone_threshold: float):
        self.db = db
        self.zone_threshold = zone_threshold
        self.minute: int | None = None
        self.zone: list[float] = []
        self.tank: list[float] = []
        self.moving_s = 0.0
        self.last_ts: float | None = None

    def add(self, ts: float, zone: float, tank: float) -> None:
        minute = int(ts // 60) * 60
        if self.minute is not None and minute != self.minute:
            self.flush()
        self.minute = minute
        if self.last_ts is not None and zone >= self.zone_threshold:
            self.moving_s += min(ts - self.last_ts, 1.0)
        self.last_ts = ts
        self.zone.append(zone)
        self.tank.append(tank)

    def flush(self) -> None:
        if self.minute is None or not self.zone:
            return
        z, t = np.array(self.zone), np.array(self.tank)
        self.db.execute("INSERT OR REPLACE INTO activity VALUES (?,?,?,?,?,?)",
                        (self.minute, float(z.max()), float(z.mean()), float(t.max()), float(t.mean()),
                         self.moving_s))
        self.zone.clear()
        self.tank.clear()
        self.moving_s = 0.0


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.data = cfg.data_path
        self.data.mkdir(parents=True, exist_ok=True)
        self.db = DB(self.data / "bubblecam.db")
        # Anything left open by a crash or power cut.
        self.db.execute("UPDATE events SET end_ts = start_ts + 60 WHERE end_ts IS NULL")
        cam = cfg.camera
        self.detector = Detector(cfg.detect, cam.width, cam.height)
        self.zones = load_zones(self.data)
        self.detector.set_zones(self.zones)
        self.events = EventManager(cfg.zone_events, cfg.burst_events, self.db, self.data / "thumbs")
        self.recorder = (Recorder(cfg.record, self.data / "rec", cam.width, cam.height, cam.fps, self.db)
                         if cfg.record.enabled else None)
        self.timelapses = TimelapseWorker(self.db, cfg)
        self.stats = MinuteStats(self.db, cfg.zone_events.threshold)

        self.lock = threading.Lock()
        self.latest: np.ndarray | None = None
        self.latest_ts = 0.0
        self.scores = Scores()
        self.fps = 0.0
        self.frame_cond = threading.Condition()
        self.running = True

    # Zones

    def update_zones(self, zones: list[dict]) -> None:
        save_zones(self.data, zones)
        self.zones = load_zones(self.data)
        with self.lock:
            self.detector.set_zones(self.zones)

    # Main loop

    def start(self) -> None:
        threading.Thread(target=self._capture_loop, name="capture", daemon=True).start()
        threading.Thread(target=self._housekeeping, name="housekeeping", daemon=True).start()

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        self.events.close_all(time.time())
        self.stats.flush()
        if self.recorder:
            self.recorder.stop()

    def _capture_loop(self) -> None:
        n = 0
        fps_t, fps_n = time.time(), 0
        scores = Scores()
        try:
            for frame, ts in open_source(self.cfg.camera):
                if not self.running:
                    break
                if n % max(1, self.cfg.detect.analyze_every) == 0:
                    with self.lock:
                        scores = self.detector.analyze(frame, ts)
                    self.events.update(scores, ts, frame)
                n += 1
                self.stats.add(ts, scores.zone, scores.tank)
                if self.recorder:
                    self.recorder.submit(frame, ts, scores.zone, scores.tank)
                with self.frame_cond:
                    self.latest, self.latest_ts, self.scores = frame, ts, scores
                    self.frame_cond.notify_all()
                fps_n += 1
                if ts - fps_t >= 2.0:
                    self.fps = fps_n / (ts - fps_t)
                    fps_t, fps_n = ts, 0
        except Exception:
            log.exception("capture loop crashed")
            raise

    def wait_frame(self, after_ts: float, timeout: float = 2.0) -> tuple[np.ndarray | None, float, Scores]:
        with self.frame_cond:
            self.frame_cond.wait_for(lambda: self.latest_ts > after_ts, timeout=timeout)
            return self.latest, self.latest_ts, self.scores

    def snapshot_jpeg(self, width: int | None = None, overlay: bool = False) -> bytes | None:
        frame, ts, scores = self.latest, self.latest_ts, self.scores
        if frame is None:
            return None
        return encode_view(frame, scores, self.zones if overlay else [], width, overlay)

    # Housekeeping: disk space

    def _housekeeping(self) -> None:
        while self.running:
            try:
                self.free_space()
            except Exception:
                log.exception("housekeeping error")
            time.sleep(300)

    def free_space(self) -> None:
        need = self.cfg.storage.min_free_gb * 1e9
        current = self.recorder.current_segment_id if self.recorder else None
        while shutil.disk_usage(self.data).free < need:
            seg = self.db.one(
                """SELECT s.* FROM segments s WHERE s.end_ts IS NOT NULL AND s.id != ? AND NOT EXISTS (
                     SELECT 1 FROM events e WHERE e.starred = 1
                       AND e.start_ts < s.end_ts AND COALESCE(e.end_ts, e.start_ts) > s.start_ts)
                   ORDER BY s.start_ts LIMIT 1""", (current or -1,))
            if not seg:
                log.warning("disk low but nothing left that can be deleted")
                return
            for f in segment_files(Path(seg["path"])):
                f.unlink(missing_ok=True)
            self.db.execute("DELETE FROM segments WHERE id=?", (seg["id"],))
            log.info("deleted old recording %s", seg["path"])


def encode_view(frame: np.ndarray, scores: Scores, zones: list[dict], width: int | None,
                overlay: bool) -> bytes:
    img = frame
    if width and width < frame.shape[1]:
        img = cv2.resize(frame, (width, round(frame.shape[0] * width / frame.shape[1])),
                         interpolation=cv2.INTER_AREA)
    if overlay:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        h, w = img.shape[:2]
        for z in zones:
            color = (80, 200, 255) if z["kind"] == "watch" else (90, 90, 90)
            p0 = (int(z["x"] * w), int(z["y"] * h))
            p1 = (int((z["x"] + z["w"]) * w), int((z["y"] + z["h"]) * h))
            cv2.rectangle(img, p0, p1, color, 2)
            cv2.putText(img, z["name"], (p0[0] + 4, p0[1] + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1,
                        cv2.LINE_AA)
        for bx, by, bw, bh in scores.boxes:
            cv2.rectangle(img, (int(bx * w), int(by * h)), (int((bx + bw) * w), int((by + bh) * h)),
                          (90, 255, 120), 1)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 75])
    return buf.tobytes()
