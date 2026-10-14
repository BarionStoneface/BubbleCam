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
    """A fake tank: textured substrate, a cave, a bubble column, a school of fast fish, a slow dark
    "pleco" that leaves the cave every so often, a shrimp on a perch that fans or forages, a snail
    crawling the glass, and switches for chases, a dead fish and night lighting."""

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
        self.bubbles_on = True
        # Night: the tank light is off (dimmer, IR look) and the school sleeps.
        self.night = False
        # Chases: (start, end) stretches where one fish darts after another.
        self.chases: list[tuple[float, float]] = []
        # A fish floating dead still at the surface from this time on (None = no).
        self.dead_from: float | None = None
        # Shrimp perch (upper right) and what it's doing: "fanning", "foraging" or "idle".
        self.perch = (int(width * 0.84), int(height * 0.22))
        self.shrimp_mode = "idle"
        # Snail: crawls slowly along the glass, about 2 px a second.
        self.snail = True

    def frame(self, t: float) -> np.ndarray:
        img = self.bg.copy()
        w, h = self.w, self.h
        # Bubble column: always moving, should be masked with an ignore zone.
        bx = int(w * 0.1)
        for i in range(8 if self.bubbles_on else 0):
            by = int((h - ((t * 120 + i * 97) % h)))
            cv2.circle(img, (bx + int(6 * math.sin(t * 3 + i)), by), 5, 200, -1)
        # School of small fast fish, mostly in the upper water column; asleep (still, low) at night.
        feeding = any(a <= t < b for a, b in self.feeds)
        for k, (phase, speed) in enumerate(self.school * (4 if feeding else 1)):
            if feeding:
                x = int(w * (0.5 + 0.45 * math.sin(t * 3.1 * speed + phase + k)))
                y = int(h * (0.08 + 0.06 * math.sin(t * 4.3 * speed + phase * 2 + k)))
                cv2.ellipse(img, (x, y), (22, 8), 0, 0, 360, 170, -1)
                continue
            if self.night:
                x = int(w * (0.3 + 0.07 * k))
                y = int(h * 0.55 + 2 * math.sin(t * 0.3 + phase))
            else:
                x = int(w * (0.5 + 0.4 * math.sin(t * 0.7 * speed + phase)))
                y = int(h * (0.3 + 0.1 * math.sin(t * 1.3 * speed + phase * 2)))
            cv2.ellipse(img, (x, y), (14, 5), 0, 0, 360, 170, -1)
        # A chase: two fish, one darting after the other, across the middle of the tank.
        for a, b in self.chases:
            if a <= t < a + 1.2:                                   # a dart: across the tank in about a second
                p = (t - a) / 1.2
                x = int(w * (0.15 + 0.7 * p))
                y = int(h * (0.45 + 0.06 * math.sin(p * 6)))
                cv2.ellipse(img, (x, y), (16, 6), 0, 0, 360, 185, -1)
                cv2.ellipse(img, (x - 42, y + 6), (16, 6), 0, 0, 360, 160, -1)
        # Dead fish: floating still at the surface, slightly drifting.
        if self.dead_from is not None and t >= self.dead_from:
            cv2.ellipse(img, (int(w * 0.45) + int(2 * math.sin(t * 0.05)), int(h * 0.05)), (16, 6), 0, 0, 360, 175, -1)
        # Shrimp on its perch.
        px, py = self.perch
        if self.shrimp_mode == "foraging":
            px = px - int(140 * (0.5 + 0.5 * math.sin(t * 0.3)))     # slow walk back and forth, 7 cm or so
            py = py + int(20 * math.sin(t * 0.5))
        sw, sh = int(w * 0.055), int(w * 0.014)                      # a 7 cm shrimp in a 60 cm tank
        cv2.ellipse(img, (px, py), (sw, sh), 0, 0, 360, 140, -1)
        if self.shrimp_mode == "fanning":
            for k in range(4):                                        # fans flicking in place
                ang = 30 * math.sin(t * 6 + k) - 60 + 25 * k
                ex = px - sw + int(w * 0.03 * math.cos(math.radians(ang)))
                ey = py + int(w * 0.03 * math.sin(math.radians(ang)))
                cv2.line(img, (px - sw, py), (ex, ey), 190, max(2, w // 400))
        # Snail: along the front glass, slowly.
        if self.snail:                                                # about a centimetre a minute
            sx = int((t * 0.25) % (w * 0.6) + w * 0.2)
            sy = int(h * 0.5 + 30 * math.sin(t * 0.002))
            cv2.circle(img, (sx, sy), 7, 110, -1)
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
        cv2.ellipse(img, pos, (int(w * 0.07), int(w * 0.022)), 0, 0, 360, 12, -1)   # a 9 cm pleco
        if self.night:
            img = (img.astype(np.float32) * 0.45).astype(np.uint8)
        noise = self.rng.integers(0, 4, img.shape, dtype=np.uint8)
        return cv2.add(img, noise)


def _synthetic_frames(cfg: CameraConfig) -> Iterator[tuple[np.ndarray, float]]:
    """For trying things without hardware. If BUBBLECAM_SYNTH_CTRL names a file containing
    "seed dx dy" plus any of these words, the fake camera obeys them while the app runs:
      feed       a feeding rush            nobubbles  the filter stops
      night      tank light off            chase      one fish darts after another
      dead       a fish floats still       fanning / foraging   what the shrimp is doing
      nocam      the camera is unplugged   freeze     the picture stops changing"""
    tanks: dict[int, SyntheticTank] = {}
    ctrl = os.environ.get("BUBBLECAM_SYNTH_CTRL")
    seed, dx, dy = 1, 0, 0
    nocam = freeze = False
    frozen = None
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
            parts: list[str] = []
            try:
                parts = open(ctrl).read().split()
                seed, dx, dy = int(parts[0]), int(parts[1]), int(parts[2])
            except (OSError, ValueError, IndexError):
                pass
            feed = "feed" in parts
            tank = tanks.setdefault(seed, SyntheticTank(cfg.width, cfg.height, seed=seed))
            tank.bubbles_on = "nobubbles" not in parts
            tank.night = "night" in parts
            tank.shrimp_mode = "fanning" if "fanning" in parts else "foraging" if "foraging" in parts else "idle"
            t = now - start
            if feed and not any(a <= t < b for a, b in tank.feeds):
                tank.feeds.append((t, t + 25))
            if "chase" in parts and not any(a <= t < b for a, b in tank.chases):
                tank.chases.append((t, t + 3))
            if "dead" in parts and tank.dead_from is None:
                tank.dead_from = t
            if "dead" not in parts:
                tank.dead_from = None
            nocam = "nocam" in parts
            freeze = "freeze" in parts
        if nocam:
            continue                      # the camera is "unplugged": no frames at all
        tank = tanks.setdefault(seed, SyntheticTank(cfg.width, cfg.height, seed=seed))
        if freeze:
            # A stuck driver hands back the very same buffer every time, noise and all.
            if frozen is None:
                frozen = tank.frame(now - start)
            frame = frozen
        else:
            frozen = None
            frame = tank.frame(now - start)
        if dx or dy:
            M = np.float32([[1, 0, dx], [0, 1, dy]])
            frame = cv2.warpAffine(frame, M, (frame.shape[1], frame.shape[0]), borderMode=cv2.BORDER_REFLECT)
        yield frame, now
