"""Continuous real time recording in segments, plus a one frame per second "proxy" stream.

Each segment is three or four files in data/rec/YYYY-MM-DD/:
  HHMMSS.mp4        real time video (H.264, keyframe every second)
  HHMMSS.act        one record per video frame: timestamp, whole tank motion, motion for up to four
                    spotlighted animals (by slot), and which tank the camera was looking at
  HHMMSS.proxy.mp4  the most active frame of each second, one per second
  HHMMSS.proxy.act  one record per proxy frame: main frame index, timestamp, best spot score, tank

The proxy is what lets the dynamic timelapse skip quiet hours without decoding every frame.
"""

from __future__ import annotations

import logging
import os
import platform
import queue
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from .config import RecordConfig
from .db import DB

log = logging.getLogger(__name__)

ACT_DTYPE = np.dtype([("ts", "<f8"), ("tank", "<f4"), ("spots", "<f4", (4,)), ("view", "<i2")])
PROXY_DTYPE = np.dtype([("idx", "<i4"), ("ts", "<f8"), ("zone", "<f4"), ("tank", "<f4")])


# Fragmented MP4: still playable if the Pi loses power mid segment. The Pi's hardware encoder can't
# supply the stream header before the first frame, which "empty_moov" needs, and the file comes out
# unreadable; without empty_moov the header is written after the first frame and it works.
FRAG = "+frag_keyframe+empty_moov+default_base_moof"
FRAG_LATE = "+frag_keyframe+default_base_moof"


def verify_mp4(path: Path) -> bool:
    """Can this video actually be read back: size known and the first frame decodes?"""
    try:
        size = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                               "stream=width,height", "-of", "csv=p=0", str(path)],
                              capture_output=True, text=True, timeout=30)
        if size.returncode != 0 or "," not in size.stdout:
            return False
        dec = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-f", "null", "-"],
                             capture_output=True, timeout=30)
        return dec.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _try_encoder(enc: str, movflags: str, width: int, height: int, fps: float) -> bool:
    """Record two seconds the way the real recorder does, then check the file reads back."""
    fd, name = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    path = Path(name)
    try:
        frames = int(max(fps, 1) * 2)
        data = b"".join(np.full((height, width), 40 + (i * 3) % 100, np.uint8).tobytes() for i in range(frames))
        r = subprocess.run(raw_encoder_cmd(path, width, height, fps, enc, "2M", max(1, round(fps)), movflags),
                           input=data, capture_output=True, timeout=60)
        return r.returncode == 0 and verify_mp4(path)
    except (OSError, subprocess.TimeoutExpired):
        return False
    finally:
        path.unlink(missing_ok=True)


def detect_encoder(preference: str, width: int = 1280, height: int = 800, fps: float = 30.0) -> tuple[str, str]:
    """Return (encoder, movflags) that produce files that can be read back. The Pi 4 hardware encoder
    shows up in `ffmpeg -encoders` even where there's no hardware, and on a Pi it can produce files
    nothing can open, so try each for real, at the real size, and check the result."""
    encs = ["h264_v4l2m2m", "libx264"] if preference == "auto" else [preference, "libx264"]
    for enc in dict.fromkeys(encs):
        for movflags in (FRAG, FRAG_LATE):
            if _try_encoder(enc, movflags, width, height, fps):
                return enc, movflags
            log.info("encoder %s with %s makes unreadable files; trying the next", enc, movflags)
    raise RuntimeError("no working H.264 encoder found (is ffmpeg installed?)")


def encoder_args(encoder: str, bitrate: str, gop: int) -> list[str]:
    args = ["-c:v", encoder, "-b:v", bitrate, "-g", str(gop), "-pix_fmt", "yuv420p"]
    if encoder == "libx264":
        # On the Pi's processor, ultrafast is what keeps up with 30 frames a second.
        fast = "ultrafast" if platform.machine() in ("aarch64", "armv7l", "arm64") else "veryfast"
        args += ["-preset", fast, "-sc_threshold", "0", "-bf", "0"]
    return args


def raw_encoder_cmd(path: Path, width: int, height: int, fps: float, encoder: str, bitrate: str,
                    gop: int, movflags: str = FRAG) -> list[str]:
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{width}x{height}", "-framerate", str(fps), "-i", "-",
            *encoder_args(encoder, bitrate, gop),
            "-movflags", movflags,
            "-f", "mp4", str(path)]


class _Segment:
    def __init__(self, rec_dir: Path, start_ts: float, width: int, height: int, fps: float,
                 encoder: str, bitrate: str, db: DB, flat: bool = False, kept: bool = False,
                 movflags: str = FRAG):
        day = datetime.fromtimestamp(start_ts)
        folder = rec_dir if flat else rec_dir / day.strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        stem = day.strftime("%H%M%S")
        self.path = folder / f"{stem}.mp4"
        self.proxy_path = folder / f"{stem}.proxy.mp4"
        self.main = subprocess.Popen(raw_encoder_cmd(self.path, width, height, fps, encoder, bitrate,
                                                     gop=max(1, round(fps)), movflags=movflags),
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
        self.seg_id = db.add_segment(str(self.path), start_ts, fps, kept)
        # Best frame of the current second, waiting to go to the proxy.
        self.bucket: int | None = None
        self.best: tuple[float, float, int, float, np.ndarray] | None = None

    def write(self, frame: np.ndarray, ts: float, tank: float, spots: np.ndarray, view: int) -> None:
        self.main.stdin.write(frame.tobytes())
        rec = np.zeros(1, ACT_DTYPE)
        rec["ts"], rec["tank"], rec["spots"], rec["view"] = ts, tank, spots, view
        self.act.write(rec.tobytes())
        zone = float(spots.max())
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
        try:
            self._flush_proxy()
        except OSError:
            pass
        for proc in (self.main, self.proxy):
            try:
                proc.stdin.close()
                proc.wait(timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
        for f in (self.act, self.proxy_act):
            try:
                f.close()
            except OSError:
                pass
        self.db.close_segment(self.seg_id, self.last_ts, self.frames)


class Recorder:
    """Takes frames on a queue so a slow disk never stalls the camera or the detector."""

    def __init__(self, cfg: RecordConfig, rec_dir: Path, width: int, height: int, fps: float, db: DB,
                 flat: bool = False, kept: bool = False):
        """flat: segments go straight into rec_dir, not a folder per day. kept: never cleared out."""
        self.cfg = cfg
        self.rec_dir = rec_dir
        self.flat, self.kept = flat, kept
        self.width, self.height, self.fps = width, height, fps
        self.db = db
        self.encoder, self.movflags = detect_encoder(cfg.encoder, width, height, fps)
        log.info("recording with %s (%s)", self.encoder, self.movflags)
        self.unreadable = 0          # finished parts that couldn't be read back
        self.q: queue.Queue = queue.Queue(maxsize=int(fps * 2))
        self.dropped = 0
        self.last_write = time.time()
        self.segment: _Segment | None = None
        self.thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self.running = True
        self.thread.start()

    def submit(self, frame: np.ndarray, ts: float, tank: float, spots: np.ndarray, view: int) -> None:
        try:
            self.q.put_nowait((frame, ts, tank, spots, view))
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
            if item == "cut":
                if self.segment:
                    self.segment.close()
                    self.segment = None
                continue
            frame, ts, tank, spots, view = item
            if self.segment is None or ts - self.segment.start_ts >= seg_len:
                if self.segment:
                    self._close_and_check(self.segment)
                self.segment = _Segment(self.rec_dir, ts, self.width, self.height, self.fps,
                                        self.encoder, self.cfg.bitrate, self.db, self.flat, self.kept,
                                        self.movflags)
            try:
                self.segment.write(frame, ts, tank, spots, view)
                self.last_write = time.time()
            except (BrokenPipeError, OSError) as e:
                log.error("recording write failed (%s); starting a new segment", e)
                try:
                    self.segment.close()
                except Exception:
                    pass
                self.segment = None
        if self.segment:
            self.segment.close()
            self.segment = None

    def _close_and_check(self, seg: "_Segment") -> None:
        seg.close()
        path = seg.path
        threading.Thread(target=self._check, args=(path,), name="rec-check", daemon=True).start()

    def _check(self, path: Path) -> None:
        """Every finished part is read back. If one can't be, the encoder is misbehaving: switch to the
        software one so the rest of tonight is watchable."""
        if not path.exists() or verify_mp4(path):
            return
        self.unreadable += 1
        log.error("recording %s can't be read back (%s, %s)", path, self.encoder, self.movflags)
        if self.encoder != "libx264" or self.movflags != FRAG:
            log.warning("switching recording to libx264")
            self.encoder, self.movflags = "libx264", FRAG

    def cut(self) -> None:
        """Finish the current segment now (recording is pausing)."""
        self.q.put("cut")

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

