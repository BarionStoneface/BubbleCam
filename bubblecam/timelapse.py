"""Dynamic timelapse: fast through quiet stretches, easing down toward real time when something moves.

How a render works:
  1. Load every frame's timestamp and motion score for the time range (from the .act sidecars).
  2. Turn motion into "interest" (0 to 1), widen it by the ease time on both sides and smooth it,
     so the speed starts dropping before the pleco appears and picks back up gently after.
  3. Map interest to speed on a log scale between max_speed and min_speed.
  4. Walk through the frames adding up output time; each output frame owns a window of input frames.
  5. From each window keep one frame: in long windows (fast parts) the most active frame, taken from
     the proxy stream so the quiet hours never need decoding; in short windows the last frame.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

from .config import Config
from .db import DB
from .media import FrameReader, probe_size
from .recorder import act_path, detect_encoder, encoder_args, proxy_paths, read_act, read_proxy_act

log = logging.getLogger(__name__)


@dataclass
class TimelapseParams:
    max_speed: float = 300.0
    min_speed: float = 2.0
    ease_seconds: float = 20.0
    driver: str = "spots"         # "spots" (all spotlighted animals), "spot:<slot>", "tank" or "both"
    output_fps: int = 30
    overlay: bool = True


def interest(spot: np.ndarray, tank: np.ndarray, driver: str, spot_ref: float, tank_ref: float) -> np.ndarray:
    si = np.clip(spot / max(spot_ref, 1e-6), 0, 1)
    ti = np.clip(tank / max(tank_ref, 1e-6), 0, 1)
    if driver == "tank":
        return ti
    if driver == "both":
        return np.maximum(si, ti)
    return si


def driver_slots(driver: str, tank) -> list[int]:
    if driver.startswith("spot:"):
        return [int(driver.split(":", 1)[1])]
    return [s.slot for s in tank.spotlights] if tank else []


def ease(ts: np.ndarray, raw: np.ndarray, ease_seconds: float) -> np.ndarray:
    """Spread interest over +/- ease_seconds and smooth it, working in one second bins."""
    if len(ts) == 0:
        return raw
    bins = (ts - ts[0]).astype(np.int64)
    nb = int(bins[-1]) + 1
    per_sec = np.zeros(nb)
    np.maximum.at(per_sec, bins, raw)
    half = max(0, int(round(ease_seconds / 2)))
    if half:
        padded = np.pad(per_sec, half)
        per_sec = np.lib.stride_tricks.sliding_window_view(padded, 2 * half + 1).max(axis=1)
        kernel = np.ones(2 * half + 1) / (2 * half + 1)
        per_sec = np.convolve(np.pad(per_sec, half, mode="edge"), kernel, mode="valid")
    return np.clip(per_sec[bins], 0, 1)


def speed_curve(smooth: np.ndarray, min_speed: float, max_speed: float) -> np.ndarray:
    min_speed = max(1.0, min_speed)
    max_speed = max(min_speed, max_speed)
    return max_speed * (min_speed / max_speed) ** smooth


def windows(ts: np.ndarray, speed: np.ndarray, output_fps: int, nominal_fps: float) -> tuple[np.ndarray, np.ndarray]:
    """Split frames into output frame windows. Returns (window start indices, window end indices), inclusive."""
    n = len(ts)
    if n == 0:
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    dt = np.empty(n)
    dt[:-1] = np.diff(ts)
    dt[-1] = 1.0 / nominal_fps
    # Gaps (camera restarts, deleted footage) collapse instead of producing frames.
    dt = np.clip(dt, 0, 2.0 / nominal_fps)
    inc = dt / speed * output_fps
    k = np.floor(np.concatenate(([0.0], np.cumsum(inc)[:-1]))).astype(np.int64)
    starts = np.flatnonzero(np.diff(k, prepend=-1))
    ends = np.append(starts[1:] - 1, n - 1)
    return starts, ends


@dataclass
class Pick:
    seg: int        # index into the segment list
    source: str     # "main" or "proxy"
    idx: int        # frame index within that file
    ts: float
    speed: float


def plan(segments: list[dict], cfg: Config, p: TimelapseParams, start_ts: float, end_ts: float,
         tank=None) -> list[Pick]:
    """tank: only use footage from while the camera was looking at this tank (None = all footage)."""
    ts_l, spot_l, tank_l, seg_l, idx_l = [], [], [], [], []
    proxies: list[np.ndarray] = []
    slots = driver_slots(p.driver, tank)
    fps = 30.0
    for si, seg in enumerate(segments):
        video = Path(seg["path"])
        act = read_act(act_path(video))
        keep = (act["ts"] >= start_ts) & (act["ts"] < end_ts)
        if tank is not None:
            keep &= act["view"] == tank.id
        sel = np.flatnonzero(keep)
        proxies.append(read_proxy_act(proxy_paths(video)[1]))
        if len(sel) == 0:
            continue
        fps = seg["fps"]
        ts_l.append(act["ts"][sel])
        spots = act["spots"][sel]
        spot_l.append(spots[:, slots].max(axis=1) if slots else np.zeros(len(sel), np.float32))
        tank_l.append(act["tank"][sel])
        seg_l.append(np.full(len(sel), si))
        idx_l.append(sel)
    if not ts_l:
        return []
    ts = np.concatenate(ts_l)
    spot = np.concatenate(spot_l).astype(np.float64)
    tankm = np.concatenate(tank_l).astype(np.float64)
    seg = np.concatenate(seg_l)
    idx = np.concatenate(idx_l)

    spot_ref = tank.zone_threshold if tank else cfg.zone_events.threshold
    raw = interest(spot, tankm, p.driver, spot_ref, cfg.burst_events.floor)
    speed = speed_curve(ease(ts, raw, p.ease_seconds), p.min_speed, p.max_speed)
    starts, ends = windows(ts, speed, p.output_fps, fps)

    # Lookup: (segment, main frame index) -> proxy frame index.
    proxy_of: dict[tuple[int, int], int] = {}
    for si, pa in enumerate(proxies):
        for j, main_idx in enumerate(pa["idx"]):
            proxy_of[(si, int(main_idx))] = j

    score = tankm if p.driver == "tank" else spot + tankm if p.driver == "both" else spot
    picks = []
    for a, b in zip(starts, ends):
        span = ts[b] - ts[a] + 1.0 / fps
        choice = None
        if span >= 1.0:
            best = -1.0
            for i in range(a, b + 1):
                j = proxy_of.get((int(seg[i]), int(idx[i])))
                if j is not None and score[i] > best:
                    best, choice = score[i], Pick(int(seg[i]), "proxy", j, float(ts[i]), float(speed[i]))
        if choice is None:
            choice = Pick(int(seg[b]), "main", int(idx[b]), float(ts[b]), float(speed[b]))
        picks.append(choice)
    return picks


def _overlay(frame: np.ndarray, ts: float, speed: float) -> np.ndarray:
    out = frame.copy()
    h = out.shape[0]
    scale = h / 800
    label = datetime.fromtimestamp(ts).strftime("%a %H:%M:%S")
    rate = "real time" if speed < 1.5 else f"{speed:.0f}x"
    for text, x in ((label, int(16 * scale)), (rate, out.shape[1] - int((len(rate) * 17 + 16) * scale))):
        y = h - int(18 * scale)
        cv2.putText(out, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8 * scale, 0, int(4 * scale) + 1, cv2.LINE_AA)
        cv2.putText(out, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8 * scale, 235, max(1, int(2 * scale)), cv2.LINE_AA)
    return out


def render(db: DB, cfg: Config, tl_id: int, tanks=None) -> None:
    row = db.one("SELECT * FROM timelapses WHERE id=?", (tl_id,))
    p = TimelapseParams(**json.loads(row["params"]))
    db.execute("UPDATE timelapses SET status='rendering' WHERE id=?", (tl_id,))
    segments = db.segments_between(row["start_ts"], row["end_ts"])
    tank = tanks.get(row["tank_id"]) if tanks is not None else None
    picks = plan(segments, cfg, p, row["start_ts"], row["end_ts"], tank)
    if not picks:
        db.execute("UPDATE timelapses SET status='failed', error='no footage in that range' WHERE id=?", (tl_id,))
        return

    out_dir = cfg.data_path / "timelapses"
    out_dir.mkdir(parents=True, exist_ok=True)
    name = datetime.fromtimestamp(row["start_ts"]).strftime("%Y-%m-%d_%H%M")
    out = out_dir / f"{tl_id}_{name}.mp4"
    w, h = probe_size(segments[picks[0].seg]["path"])
    enc = detect_encoder(cfg.record.encoder)
    proc = subprocess.Popen(
        ["nice", "-n", "15", "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{w}x{h}", "-framerate", str(p.output_fps), "-i", "-",
         *encoder_args(enc, "4M", p.output_fps * 2), "-movflags", "+faststart", str(out)],
        stdin=subprocess.PIPE)

    readers: dict[tuple[int, str], FrameReader] = {}
    current_seg = -1
    written = 0
    try:
        for n, pick in enumerate(picks):
            if pick.seg != current_seg:
                for r in readers.values():
                    r.close()
                readers.clear()
                current_seg = pick.seg
            key = (pick.seg, pick.source)
            if key not in readers:
                video = Path(segments[pick.seg]["path"])
                if pick.source == "main":
                    readers[key] = FrameReader(video, segments[pick.seg]["fps"])
                else:
                    readers[key] = FrameReader(proxy_paths(video)[0], 1.0, seek_gap=5)
            frame = readers[key].get(pick.idx)
            if frame is None or frame.shape != (h, w):
                continue
            if p.overlay:
                frame = _overlay(frame, pick.ts, pick.speed)
            proc.stdin.write(frame.tobytes())
            written += 1
            if n % 150 == 0:
                db.execute("UPDATE timelapses SET progress=? WHERE id=?", (n / len(picks), tl_id))
        proc.stdin.close()
        proc.wait()
    finally:
        for r in readers.values():
            r.close()
        if proc.poll() is None:
            proc.kill()
    if proc.returncode == 0 and written:
        db.execute("UPDATE timelapses SET status='done', progress=1, path=? WHERE id=?", (str(out), tl_id))
    else:
        db.execute("UPDATE timelapses SET status='failed', error='encoder failed' WHERE id=?", (tl_id,))


class TimelapseWorker:
    """Renders queued timelapses one at a time, and queues each tank's nightly one."""

    def __init__(self, db: DB, cfg: Config, tanks):
        self.db, self.cfg, self.tanks = db, cfg, tanks
        self.wake = threading.Event()
        self.last_nightly: str | None = None
        db.execute("UPDATE timelapses SET status='queued' WHERE status='rendering'")
        threading.Thread(target=self._run, name="timelapse", daemon=True).start()

    def queue(self, tank_id: int | None, start_ts: float, end_ts: float, params: TimelapseParams,
              auto: bool = False) -> int:
        tl_id = self.db.add_timelapse(tank_id, start_ts, end_ts, asdict(params), time.time(), auto)
        self.wake.set()
        return tl_id

    def default_params(self) -> TimelapseParams:
        t = self.cfg.timelapse
        return TimelapseParams(max_speed=t.max_speed, min_speed=t.min_speed, ease_seconds=t.ease_seconds,
                               driver=t.driver, output_fps=t.output_fps)

    def _check_nightly(self) -> None:
        t = self.cfg.timelapse
        if not t.nightly_at:
            return
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if self.last_nightly == today or now.strftime("%H:%M") < t.nightly_at:
            return
        self.last_nightly = today
        end = datetime.combine(now.date(), datetime.strptime(t.night_end, "%H:%M").time())
        start = datetime.combine(now.date(), datetime.strptime(t.night_start, "%H:%M").time())
        if start >= end:
            start -= timedelta(days=1)
        for tank in self.tanks.all():
            # Only tanks the camera actually watched for a while last night.
            watched = sum(b - a for a, b in self.db.tank_intervals(tank.id, start.timestamp(), end.timestamp()))
            if watched < 600:
                continue
            exists = self.db.one("SELECT id FROM timelapses WHERE tank_id=? AND start_ts=? AND end_ts=?",
                                 (tank.id, start.timestamp(), end.timestamp()))
            if not exists:
                p = self.default_params()
                if not tank.spotlights:
                    p.driver = "tank"
                self.queue(tank.id, start.timestamp(), end.timestamp(), p, auto=True)

    def _run(self) -> None:
        while True:
            job = None
            try:
                self._check_nightly()
                job = self.db.one("SELECT id FROM timelapses WHERE status='queued' ORDER BY id LIMIT 1")
                if job:
                    log.info("rendering timelapse %s", job["id"])
                    render(self.db, self.cfg, job["id"], self.tanks)
                    continue
            except Exception as e:  # keep the worker alive whatever happens
                log.exception("timelapse worker error")
                if job:
                    self.db.execute("UPDATE timelapses SET status='failed', error=? WHERE id=?", (str(e), job["id"]))
            self.wake.wait(timeout=30)
            self.wake.clear()
