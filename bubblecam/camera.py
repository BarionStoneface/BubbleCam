"""Frame sources: the real USB camera, a synthetic tank for testing, or a video file."""

from __future__ import annotations

import logging
import math
import os
import subprocess
import time
from typing import Iterator

import cv2
import numpy as np

from .config import CameraConfig

log = logging.getLogger(__name__)


def open_source(cfg: CameraConfig) -> Iterator[tuple[np.ndarray, float]]:
    """Yield (grey frame, wall clock timestamp) forever."""
    if cfg.source == "v4l2":
        return _v4l2_frames(cfg)
    if cfg.source == "synthetic":
        return _synthetic_frames(cfg)
    return _file_frames(cfg)


def _set_v4l2_controls(cfg: CameraConfig) -> None:
    # OpenCV's exposure properties are unreliable across UVC cameras; v4l2-ctl talks to the driver directly.
    # Control names differ between kernel versions, so try both spellings and ignore failures.
    if not cfg.manual_exposure:
        return
    attempts = [
        ["-c", "auto_exposure=1", "-c", f"exposure_time_absolute={cfg.exposure}"],
        ["-c", "exposure_auto=1", "-c", f"exposure_absolute={cfg.exposure}"],
        ["-c", f"gain={cfg.gain}"],
    ]
    for args in attempts:
        try:
            subprocess.run(["v4l2-ctl", "-d", cfg.device, *args], capture_output=True, timeout=5)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            log.warning("v4l2-ctl not available; install v4l-utils to lock exposure")
            return


def _v4l2_frames(cfg: CameraConfig) -> Iterator[tuple[np.ndarray, float]]:
    while True:
        cap = cv2.VideoCapture(cfg.device, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
        cap.set(cv2.CAP_PROP_FPS, cfg.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)
        if not cap.isOpened():
            log.error("cannot open camera %s, retrying in 5 s", cfg.device)
            time.sleep(5)
            continue
        _set_v4l2_controls(cfg)
        log.info("camera open: %dx%d @ %s fps",
                 cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT), cap.get(cv2.CAP_PROP_FPS))
        failures = 0
        while failures < 30:
            ok, frame = cap.read()
            if not ok:
                failures += 1
                time.sleep(0.05)
                continue
            failures = 0
            if frame.ndim == 3:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if frame.shape[1] != cfg.width or frame.shape[0] != cfg.height:
                frame = cv2.resize(frame, (cfg.width, cfg.height))
            yield frame, time.time()
        log.error("camera stopped delivering frames; reopening")
        cap.release()
        time.sleep(2)


def _file_frames(cfg: CameraConfig) -> Iterator[tuple[np.ndarray, float]]:
    period = 1.0 / cfg.fps
    while True:
        cap = cv2.VideoCapture(cfg.source)
        if not cap.isOpened():
            raise RuntimeError(f"cannot open video file {cfg.source}")
        next_t = time.time()
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if frame.ndim == 3:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            frame = cv2.resize(frame, (cfg.width, cfg.height))
            next_t += period
            delay = next_t - time.time()
            if delay > 0:
                time.sleep(delay)
            yield frame, time.time()
        cap.release()


class SyntheticTank:
    """A fake night tank: textured substrate, a cave, a bubble column, a small school of
    fast fish, and a slow dark "pleco" that leaves the cave every so often."""

    def __init__(self, width: int, height: int, seed: int = 1):
        self.w, self.h = width, height
        rng = np.random.default_rng(seed)
        grad = np.linspace(40, 90, height, dtype=np.float32)[:, None]
        noise = rng.normal(0, 6, (height // 8, width // 8)).astype(np.float32)
        noise = cv2.resize(noise, (width, height), interpolation=cv2.INTER_CUBIC)
        bg = grad + noise
        bg[int(height * 0.8):] += 40  # sand
        self.cave = (int(width * 0.62), int(height * 0.66), int(width * 0.14), int(height * 0.14))
        cx, cy, cw, ch = self.cave
        bg[cy:cy + ch, cx:cx + cw] = 22
        self.bg = np.clip(bg, 0, 255).astype(np.uint8)
        self.rng = rng
        self.school = [(rng.uniform(0, 6.28), rng.uniform(0.6, 1.4)) for _ in range(6)]
        # Pleco schedule (seconds within a repeating cycle): out from 40 s to 70 s.
        self.cycle = 120.0
        self.out_from, self.out_to = 40.0, 70.0
        # Feeding: (start, end) times in seconds; the school rushes to the surface and goes wild.
        self.feeds: list[tuple[float, float]] = []

    def frame(self, t: float) -> np.ndarray:
        img = self.bg.copy()
        w, h = self.w, self.h
        # Bubble column: always moving, should be masked with an ignore zone.
        bx = int(w * 0.1)
        for i in range(8):
            by = int((h - ((t * 120 + i * 97) % h)))
            cv2.circle(img, (bx + int(6 * math.sin(t * 3 + i)), by), 5, 200, -1)
        # School of small fast fish, mostly in the upper water column.
        feeding = any(a <= t < b for a, b in self.feeds)
        for k, (phase, speed) in enumerate(self.school * (4 if feeding else 1)):
            if feeding:
                x = int(w * (0.5 + 0.45 * math.sin(t * 3.1 * speed + phase + k)))
                y = int(h * (0.08 + 0.06 * math.sin(t * 4.3 * speed + phase * 2 + k)))
                cv2.ellipse(img, (x, y), (22, 8), 0, 0, 360, 170, -1)
                continue
            x = int(w * (0.5 + 0.4 * math.sin(t * 0.7 * speed + phase)))
            y = int(h * (0.3 + 0.1 * math.sin(t * 1.3 * speed + phase * 2)))
            cv2.ellipse(img, (x, y), (14, 5), 0, 0, 360, 170, -1)
        # Pleco: emerges from the cave, wanders on the sand, goes back.
        cx, cy, cw, ch = self.cave
        home = (cx + cw // 2, cy + ch // 2)
        c = t % self.cycle
        if self.out_from <= c <= self.out_to:
            p = (c - self.out_from) / (self.out_to - self.out_from)
            dx = int(math.sin(p * math.pi) * w * 0.25)
            pos = (home[0] - dx, home[1] + int(h * 0.08 * math.sin(p * math.pi)))
        else:
            pos = (home[0] + int(3 * math.sin(t * 0.5)), home[1])
        cv2.ellipse(img, pos, (40, 14), 0, 0, 360, 12, -1)
        noise = self.rng.integers(0, 4, img.shape, dtype=np.uint8)
        return cv2.add(img, noise)


def _synthetic_frames(cfg: CameraConfig) -> Iterator[tuple[np.ndarray, float]]:
    """For trying things without hardware. If BUBBLECAM_SYNTH_CTRL names a file containing
    "seed dx dy [feed]", the fake camera switches tank (seed), shifts by dx, dy pixels, and with
    "feed" puts on a feeding rush: handy for testing tank recognition and feeding detection."""
    tanks: dict[int, SyntheticTank] = {}
    ctrl = os.environ.get("BUBBLECAM_SYNTH_CTRL")
    seed, dx, dy = 1, 0, 0
    period = 1.0 / cfg.fps
    start = time.time()
    next_t = start
    last_ctrl = 0.0
    while True:
        next_t += period
        delay = next_t - time.time()
        if delay > 0:
            time.sleep(delay)
        now = time.time()
        if ctrl and now - last_ctrl > 1:
            last_ctrl = now
            try:
                parts = open(ctrl).read().split()
                seed, dx, dy = int(parts[0]), int(parts[1]), int(parts[2])
                feed = "feed" in parts
            except (OSError, ValueError, IndexError):
                feed = False
            tank = tanks.setdefault(seed, SyntheticTank(cfg.width, cfg.height, seed=seed))
            t = now - start
            if feed and not any(a <= t < b for a, b in tank.feeds):
                tank.feeds.append((t, t + 25))
        tank = tanks.setdefault(seed, SyntheticTank(cfg.width, cfg.height, seed=seed))
        frame = tank.frame(now - start)
        if dx or dy:
            M = np.float32([[1, 0, dx], [0, 1, dy]])
            frame = cv2.warpAffine(frame, M, (frame.shape[1], frame.shape[0]), borderMode=cv2.BORDER_REFLECT)
        yield frame, now
