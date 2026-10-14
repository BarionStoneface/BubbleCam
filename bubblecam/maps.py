"""Pictures of where things happen: movement heatmaps, and the paths of slow movers.

Heatmap   every analysed frame's moving pixels are added up, per tank, separately for day and night,
          one file per date. Drawn over the tank's latest picture on request.

Trails    once a night is over, the proxy stream (one frame a second) is sampled a minute apart.
          Anything present in the same place, give or take, minute after minute is a slow mover: a
          snail on the glass, a pleco grazing, a shrimp walking. Fast fish are somewhere else every
          minute and vanish. Each slow mover's path is drawn on a picture of the empty tank.
"""

from __future__ import annotations

import logging
import math
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

from .db import DB
from .recorder import proxy_paths

log = logging.getLogger(__name__)

HEAT_SAVE_EVERY = 300
TRAIL_W = 480
TRAIL_DIFF_MIN = 8           # a change has to be at least this many grey levels...
TRAIL_DIFF_SHARE = 0.2       # ...or this share of the picture's own contrast, whichever is more
TRAIL_MIN_AREA = 12          # pixels at TRAIL_W wide
TRAIL_MAX_AREA = 2500
TRAIL_MIN_LINKS = 6          # minutes something has to be followed for
TRAIL_MAX_STEP = 8.0         # average pixels per minute (at 480 wide); faster isn't a slow mover
TRAIL_BRIDGE_GAP = 4         # a path lost for up to this many minutes is joined back up
TRAIL_COLOURS = [(95, 208, 192), (240, 180, 90), (217, 138, 224), (143, 212, 106), (127, 180, 240), (233, 135, 107)]


def _night_window(ts: float, night_start: str, night_end: str) -> tuple[str, str]:
    """('night', date) or ('day', date) for a moment in time. A night is named after the date it starts."""
    dt = datetime.fromtimestamp(ts)
    ns = datetime.strptime(night_start, "%H:%M").time()
    ne = datetime.strptime(night_end, "%H:%M").time()
    t = dt.time()
    if ns > ne:                      # the usual case: the night crosses midnight
        if t >= ns:
            return "night", dt.strftime("%Y-%m-%d")
        if t < ne:
            return "night", (dt - timedelta(days=1)).strftime("%Y-%m-%d")
        return "day", dt.strftime("%Y-%m-%d")
    if ns <= t < ne:
        return "night", dt.strftime("%Y-%m-%d")
    return "day", dt.strftime("%Y-%m-%d")


class HeatMaps:
    def __init__(self, data_dir: Path, night_start: str, night_end: str):
        self.dir = data_dir / "tanks"
        self.night_start, self.night_end = night_start, night_end
        self.lock = threading.Lock()
        self.current: dict[tuple[int, str, str], np.ndarray] = {}     # (tank, period, date) -> heat
        self.dirty: set[tuple[int, str, str]] = set()
        self.last_save = time.time()

    def _path(self, tank_id: int, period: str, date: str) -> Path:
        return self.dir / str(tank_id) / "heat" / f"{date}_{period}.npy"

    def add(self, tank_id: int, ts: float, mask: np.ndarray | None) -> None:
        if mask is None:
            return
        period, date = _night_window(ts, self.night_start, self.night_end)
        key = (tank_id, period, date)
        with self.lock:
            heat = self.current.get(key)
            if heat is None or heat.shape != mask.shape:
                p = self._path(*key)
                heat = np.load(p) if p.exists() else np.zeros(mask.shape, np.float32)
                if heat.shape != mask.shape:
                    heat = np.zeros(mask.shape, np.float32)
                # Keep only today's and yesterday's in memory.
                for k in [k for k in self.current if k[2] < (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")]:
                    self.current.pop(k, None)
                self.current[key] = heat
            heat += mask
            self.dirty.add(key)
        if time.time() - self.last_save > HEAT_SAVE_EVERY:
            self.save()

    def save(self) -> None:
        with self.lock:
            keys, self.dirty = self.dirty, set()
            items = [(k, self.current[k].copy()) for k in keys if k in self.current]
        for key, heat in items:
            p = self._path(*key)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp.npy")
            np.save(tmp, heat)
            tmp.replace(p)
        self.last_save = time.time()

    def render(self, tank_id: int, period: str, days: int, background: np.ndarray | None) -> bytes | None:
        self.save()
        folder = self.dir / str(tank_id) / "heat"
        if not folder.exists():
            return None
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        total = None
        for p in sorted(folder.glob(f"*_{period}.npy")):
            date = p.name.split("_")[0]
            if date < since:
                continue
            heat = np.load(p)
            total = heat if total is None else total + heat if total.shape == heat.shape else total
        if total is None or total.max() <= 0:
            return None
        norm = np.log1p(total) / np.log1p(total.max())
        colour = cv2.applyColorMap((norm * 255).astype(np.uint8), cv2.COLORMAP_INFERNO)
        if background is not None:
            bg = cv2.resize(background, (colour.shape[1], colour.shape[0]), interpolation=cv2.INTER_AREA)
            bg = cv2.cvtColor(bg, cv2.COLOR_GRAY2BGR)
            alpha = np.clip(norm * 1.4, 0, 0.85)[..., None]
            colour = (colour * alpha + bg * (1 - alpha)).astype(np.uint8)
        colour = cv2.resize(colour, (960, round(960 * colour.shape[0] / colour.shape[1])), interpolation=cv2.INTER_CUBIC)
        ok, buf = cv2.imencode(".png", colour)
        return buf.tobytes() if ok else None


def _keyframes(video: Path, every: int, width: int) -> list[np.ndarray]:
    """Grey frames from a proxy stream, decoding only keyframes and keeping one in `every`."""
    cmd = ["nice", "-n", "15", "ffmpeg", "-hide_banner", "-loglevel", "error", "-skip_frame", "nokey",
           "-i", str(video), "-vf", f"select=not(mod(n\\,{every})),scale={width}:-2", "-vsync", "0",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    out = subprocess.run(cmd, capture_output=True, timeout=1800).stdout
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0", str(video)], capture_output=True, text=True).stdout.strip()
    w, h = (int(v) for v in probe.split(",")[:2])
    sh = round(h * width / w)
    sh -= sh % 2
    n = len(out) // (width * sh)
    return [np.frombuffer(out[i * width * sh:(i + 1) * width * sh], np.uint8).reshape(sh, width) for i in range(n)]


def trails_from_frames(frames: list[np.ndarray]) -> tuple[np.ndarray, list[list[tuple[int, int]]]] | None:
    """Frames a minute apart -> (picture with paths drawn, the paths). None if too few frames."""
    if len(frames) < TRAIL_MIN_LINKS + 2:
        return None
    shape = frames[0].shape
    frames = [f for f in frames if f.shape == shape]
    sample = frames[::max(1, len(frames) // 60)]
    median = np.median(np.stack(sample), axis=0).astype(np.uint8)
    medianf = median.astype(np.float32)
    thr = max(TRAIL_DIFF_MIN, TRAIL_DIFF_SHARE * float(medianf.std()))

    # Blobs per minute, then chains: a blob links to one in the previous minute if it's close.
    chains: list[list[tuple[int, int, int]]] = []                     # points are (x, y, minute)
    open_chains: list[tuple[list, tuple[int, int], float]] = []      # (chain, last pos, size)
    for fi, f in enumerate(frames):
        diff = cv2.absdiff(f.astype(np.float32), medianf)
        mask = (diff > thr).astype(np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        n, labels, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
        blobs = [((int(cents[i][0]), int(cents[i][1])), float(stats[i, cv2.CC_STAT_AREA]))
                 for i in range(1, n) if TRAIL_MIN_AREA <= stats[i, cv2.CC_STAT_AREA] <= TRAIL_MAX_AREA]
        next_open = []
        used = set()
        for chain, (px, py), size in open_chains:
            reach = max(12.0, 2.5 * math.sqrt(size))
            best = None
            for j, ((cx, cy), area) in enumerate(blobs):
                if j in used:
                    continue
                d = math.hypot(cx - px, cy - py)
                if d <= reach and (best is None or d < best[0]):
                    best = (d, j)
            if best is not None:
                j = best[1]
                used.add(j)
                chain.append((*blobs[j][0], fi))
                next_open.append((chain, blobs[j][0], blobs[j][1]))
            elif len(chain) >= 2:
                chains.append(chain)
        for j, (pos, area) in enumerate(blobs):
            if j not in used:
                next_open.append(([(*pos, fi)], pos, area))
        open_chains = next_open
    chains.extend(c for c, _, _ in open_chains if len(c) >= 2)
    chains = _bridge(chains)
    chains = [c for c in chains if len(c) >= TRAIL_MIN_LINKS and _slow(c)]

    img = cv2.cvtColor(median, cv2.COLOR_GRAY2BGR)
    img = (img * 0.75).astype(np.uint8)
    for k, chain in enumerate(sorted(chains, key=len, reverse=True)):
        colour = TRAIL_COLOURS[k % len(TRAIL_COLOURS)]
        pts = np.array([(x, y) for x, y, _ in chain], np.int32).reshape(-1, 1, 2)
        cv2.polylines(img, [pts], False, colour, 2, cv2.LINE_AA)
        cv2.circle(img, chain[0][:2], 3, colour, -1)
        cv2.circle(img, chain[-1][:2], 5, colour, 2)
    label = f"{len(chains)} slow mover{'s' if len(chains) != 1 else ''}, {len(frames)} minutes"
    cv2.putText(img, label, (10, img.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (230, 230, 230), 1, cv2.LINE_AA)
    return cv2.resize(img, (960, round(960 * img.shape[0] / img.shape[1])), interpolation=cv2.INTER_CUBIC), chains


def _slow(chain) -> bool:
    steps = [math.hypot(b[0] - a[0], b[1] - a[1]) / max(1, b[2] - a[2]) for a, b in zip(chain, chain[1:])]
    path = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(chain, chain[1:]))
    return sum(steps) / len(steps) <= TRAIL_MAX_STEP and path >= 10


def _bridge(chains):
    """Join a path's pieces back up when it was lost for a few minutes (a fish passed in front)."""
    chains = sorted(chains, key=lambda c: c[0][2])
    out = []
    for c in chains:
        for prev in out:
            ex, ey, ef = prev[-1]
            sx, sy, sf = c[0]
            if 0 < sf - ef <= TRAIL_BRIDGE_GAP and math.hypot(sx - ex, sy - ey) <= TRAIL_MAX_STEP * (sf - ef) * 1.5:
                prev.extend(c)
                break
        else:
            out.append(list(c))
    return out


def trails_image(db: DB, tank, start_ts: float, end_ts: float) -> np.ndarray | None:
    """The slow movers' paths for one night, drawn on the empty tank. None if there's no footage."""
    frames: list[np.ndarray] = []
    for a, b in db.tank_intervals(tank.id, start_ts, end_ts):
        for seg in db.segments_between(a, b):
            proxy = proxy_paths(Path(seg["path"]))[0]
            if proxy.exists():
                # Proxy keyframes are every 10 s; every 6th is a minute apart.
                frames.extend(_keyframes(proxy, 6, TRAIL_W))
    result = trails_from_frames(frames)
    return result[0] if result else None


class TrailsWorker:
    """Draws each tank's trails once its night is over."""

    def __init__(self, db: DB, tanks, data_dir: Path, night_start: str, night_end: str):
        self.db, self.tanks = db, tanks
        self.dir = data_dir / "tanks"
        self.night_start, self.night_end = night_start, night_end
        self.wanted: list[tuple[int, str]] = []
        self.lock = threading.Lock()

    def path(self, tank_id: int, date: str) -> Path:
        return self.dir / str(tank_id) / "trails" / f"{date}.png"

    def request(self, tank_id: int, date: str) -> None:
        with self.lock:
            if (tank_id, date) not in self.wanted:
                self.wanted.append((tank_id, date))

    def start(self) -> None:
        threading.Thread(target=self._run, name="trails", daemon=True).start()

    def _bounds(self, date: str) -> tuple[float, float]:
        from .summary import night_bounds
        day = datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)
        return night_bounds(day, self.night_start, self.night_end)

    def _run(self) -> None:
        time.sleep(60)
        while True:
            try:
                self._check_last_night()
                with self.lock:
                    job = self.wanted.pop(0) if self.wanted else None
                if job:
                    self.make(*job)
                    continue
            except Exception:
                log.exception("trails failed")
            time.sleep(60)

    def _check_last_night(self) -> None:
        now = datetime.now()
        ne = datetime.strptime(self.night_end, "%H:%M").time()
        if now.time() < ne:
            return
        date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        for tank in self.tanks.all():
            p = self.path(tank.id, date)
            if p.exists() or p.with_suffix(".none").exists():
                continue
            self.request(tank.id, date)

    def make(self, tank_id: int, date: str) -> Path | None:
        tank = self.tanks.get(tank_id)
        if tank is None:
            return None
        start, end = self._bounds(date)
        watched = sum(b - a for a, b in self.db.tank_intervals(tank_id, start, end))
        p = self.path(tank_id, date)
        p.parent.mkdir(parents=True, exist_ok=True)
        if watched < 3600:
            p.with_suffix(".none").touch()
            return None
        log.info("drawing trails for %s, night of %s", tank.name, date)
        img = trails_image(self.db, tank, start, end)
        if img is None:
            p.with_suffix(".none").touch()
            return None
        cv2.imwrite(str(p), img)
        return p
