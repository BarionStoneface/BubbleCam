"""Continuous real time recording in segments, plus a one frame per second "proxy" stream.

Each segment is three or four files in data/rec/YYYY-MM-DD/:
  HHMMSS.mp4        real time video (H.264, keyframe every second)
  HHMMSS.act        one record per video frame: timestamp, zone score, tank score
  HHMMSS.proxy.mp4  the most active frame of each second, one per second
  HHMMSS.proxy.act  one record per proxy frame: main frame index, timestamp, zone, tank

The proxy is what lets the dynamic timelapse skip quiet hours without decoding every frame.
"""

from __future__ import annotations

import logging
import queue
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import numpy as np

from .config import RecordConfig
from .db import DB

log = logging.getLogger(__name__)

ACT_DTYPE = np.dtype([("ts", "<f8"), ("zone", "<f4"), ("tank", "<f4")])
PROXY_DTYPE = np.dtype([("idx", "<i4"), ("ts", "<f8"), ("zone", "<f4"), ("tank", "<f4")])


def detect_encoder(preference: str) -> str:
    """Return a working H.264 encoder name. The Pi 4 hardware encoder shows up in
    `ffmpeg -encoders` even on machines without the hardware, so actually try it."""
    candidates = ["h264_v4l2m2m", "libx264"] if preference == "auto" else [preference]
    for enc in candidates:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
               "testsrc=size=320x240:rate=10", "-frames:v", "10", "-c:v", enc, "-pix_fmt", "yuv420p",
               "-f", "null", "-"]
        try:
            if subprocess.run(cmd, capture_output=True, timeout=20).returncode == 0:
                return enc
        except subprocess.TimeoutExpired:
            pass
    raise RuntimeError("no working H.264 encoder found (is ffmpeg installed?)")


def encoder_args(encoder: str, bitrate: str, gop: int) -> list[str]:
    args = ["-c:v", encoder, "-b:v", bitrate, "-g", str(gop), "-pix_fmt", "yuv420p"]
    if encoder == "libx264":
        args += ["-preset", "veryfast", "-sc_threshold", "0", "-bf", "0"]
    return args


def raw_encoder_cmd(path: Path, width: int, height: int, fps: float, encoder: str, bitrate: str,
                    gop: int) -> list[str]:
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{width}x{height}", "-framerate", str(fps), "-i", "-",
            *encoder_args(encoder, bitrate, gop),
            # Fragmented MP4: still playable if the Pi loses power mid segment.
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4", str(path)]


class _Segment:
    def __init__(self, rec_dir: Path, start_ts: float, width: int, height: int, fps: float,
                 encoder: str, bitrate: str, db: DB):
        day = datetime.fromtimestamp(start_ts)
        folder = rec_dir / day.strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        stem = day.strftime("%H%M%S")
        self.path = folder / f"{stem}.mp4"
        self.proxy_path = folder / f"{stem}.proxy.mp4"
        self.main = subprocess.Popen(raw_encoder_cmd(self.path, width, height, fps, encoder, bitrate,
                                                     gop=max(1, round(fps))),
                                     stdin=subprocess.PIPE)
        # Proxy is 1 fps, so libx264 costs next to nothing and leaves the hardware encoder to the main stream.
        self.proxy = subprocess.Popen(raw_encoder_cmd(self.proxy_path, width, height, 1, "libx264", bitrate,
                                                      gop=10),
                                      stdin=subprocess.PIPE)
        self.act = open(folder / f"{stem}.act", "wb")
        self.proxy_act = open(folder / f"{stem}.proxy.act", "wb")
        self.start_ts = start_ts
        self.frames = 0
        self.last_ts = start_ts
        self.db = db
        self.seg_id = db.add_segment(str(self.path), start_ts, fps)
        # Best frame of the current second, waiting to go to the proxy.
        self.bucket: int | None = None
        self.best: tuple[float, float, int, float, np.ndarray] | None = None

    def write(self, frame: np.ndarray, ts: float, zone: float, tank: float) -> None:
        self.main.stdin.write(frame.tobytes())
        rec = np.array([(ts, zone, tank)], ACT_DTYPE)
        self.act.write(rec.tobytes())
        idx = self.frames
        self.frames += 1
        self.last_ts = ts

        bucket = int(ts)
        if self.bucket is not None and bucket != self.bucket:
            self._flush_proxy()
        self.bucket = bucket
        if self.best is None or (zone, tank) > (self.best[0], self.best[1]):
            self.best = (zone, tank, idx, ts, frame.copy())

    def _flush_proxy(self) -> None:
        if self.best is None:
            return
        zone, tank, idx, ts, frame = self.best
        self.proxy.stdin.write(frame.tobytes())
        self.proxy_act.write(np.array([(idx, ts, zone, tank)], PROXY_DTYPE).tobytes())
        self.best = None

    def close(self) -> None:
        self._flush_proxy()
        for proc in (self.main, self.proxy):
            try:
                proc.stdin.close()
                proc.wait(timeout=30)
            except (BrokenPipeError, subprocess.TimeoutExpired):
                proc.kill()
        self.act.close()
        self.proxy_act.close()
        self.db.close_segment(self.seg_id, self.last_ts, self.frames)


class Recorder:
    """Takes frames on a queue so a slow disk never stalls the camera or the detector."""

    def __init__(self, cfg: RecordConfig, rec_dir: Path, width: int, height: int, fps: float, db: DB):
        self.cfg = cfg
        self.rec_dir = rec_dir
        self.width, self.height, self.fps = width, height, fps
        self.db = db
        self.encoder = detect_encoder(cfg.encoder)
        log.info("recording with %s", self.encoder)
        self.q: queue.Queue = queue.Queue(maxsize=int(fps * 2))
        self.dropped = 0
        self.segment: _Segment | None = None
        self.thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self.running = True
        self.thread.start()

    def submit(self, frame: np.ndarray, ts: float, zone: float, tank: float) -> None:
        try:
            self.q.put_nowait((frame, ts, zone, tank))
        except queue.Full:
            self.dropped += 1
            if self.dropped % 100 == 1:
                log.warning("recorder falling behind, %d frames dropped", self.dropped)

    def _run(self) -> None:
        seg_len = self.cfg.segment_minutes * 60
        while self.running:
            try:
                item = self.q.get(timeout=1)
            except queue.Empty:
                continue
            if item is None:
                break
            frame, ts, zone, tank = item
            if self.segment is None or ts - self.segment.start_ts >= seg_len:
                if self.segment:
                    self.segment.close()
                self.segment = _Segment(self.rec_dir, ts, self.width, self.height, self.fps,
                                        self.encoder, self.cfg.bitrate, self.db)
            try:
                self.segment.write(frame, ts, zone, tank)
            except BrokenPipeError:
                log.error("encoder died; starting a new segment")
                self.segment = None
        if self.segment:
            self.segment.close()
            self.segment = None

    def stop(self) -> None:
        self.q.put(None)
        self.thread.join(timeout=60)
        self.running = False

    @property
    def current_segment_id(self) -> int | None:
        return self.segment.seg_id if self.segment else None


def read_act(path: Path) -> np.ndarray:
    return np.fromfile(path, ACT_DTYPE) if path.exists() else np.zeros(0, ACT_DTYPE)


def read_proxy_act(path: Path) -> np.ndarray:
    return np.fromfile(path, PROXY_DTYPE) if path.exists() else np.zeros(0, PROXY_DTYPE)


def act_path(video: Path) -> Path:
    return video.with_suffix(".act")


def proxy_paths(video: Path) -> tuple[Path, Path]:
    stem = video.with_suffix("")
    return Path(f"{stem}.proxy.mp4"), Path(f"{stem}.proxy.act")


def segment_files(video: Path) -> list[Path]:
    return [video, act_path(video), *proxy_paths(video)]

