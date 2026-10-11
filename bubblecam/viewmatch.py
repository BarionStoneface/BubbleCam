"""Which tank is the camera looking at, and has it shifted?

Each tank keeps a few reference pictures (taken when it was set up, plus extra ones under different
lighting). Every few seconds the current picture is compared with them using ORB features: distinctive
corners in rocks, wood, gravel and decorations. Matching corners between two pictures gives both an
answer to "same tank?" (how many corners agree) and exactly how the camera moved (a shift, a small
turn, a little zoom). Fish move around, but they're a small share of the corners and the matching
ignores what doesn't fit.

  same tank, tiny shift   -> nothing to do
  same tank, moved a bit  -> boxes are moved to follow
  same tank, moved a lot  -> boxes are moved, and Setup suggests checking them
  another known tank      -> switch to that tank's settings
  nothing matches         -> "I don't recognise this view"
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .db import DB
from .tanks import IDENTITY

log = logging.getLogger(__name__)

WORK_W = 640
N_FEATURES = 800
RATIO = 0.75
MIN_INLIERS = 25            # below this it's not a confident match
MAX_REFS = 6
NEW_REF_BRIGHTNESS = 30.0   # add a reference when lighting differs this much from all existing ones
ADJUST_EPS = 0.004          # transform changes smaller than this are noise
FAR_SHIFT = 0.2             # fraction of the frame
SWITCH_CHECKS = 3
UNKNOWN_CHECKS = 6

_clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
_orb = cv2.ORB_create(nfeatures=N_FEATURES, fastThreshold=12)
_matcher = cv2.BFMatcher(cv2.NORM_HAMMING)


@dataclass
class Features:
    pts: np.ndarray        # N x 2, pixels in the WORK_W frame
    des: np.ndarray | None
    size: tuple[int, int]  # (w, h) of the work frame
    brightness: float


def features(frame: np.ndarray) -> Features:
    h = round(frame.shape[0] * WORK_W / frame.shape[1])
    small = cv2.resize(frame, (WORK_W, h), interpolation=cv2.INTER_AREA)
    kps, des = _orb.detectAndCompute(_clahe.apply(small), None)
    pts = np.array([k.pt for k in kps], np.float32).reshape(-1, 2)
    return Features(pts, des, (WORK_W, h), float(small.mean()))


def compose(T: np.ndarray, B: np.ndarray) -> np.ndarray:
    """Affine T after B."""
    return np.hstack([T[:, :2] @ B[:, :2], (T[:, :2] @ B[:, 2:] + T[:, 2:])])


def px_to_norm(M: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    w, h = size
    S = np.array([[w, 0, 0], [0, h, 0], [0, 0, 1]], float)
    Si = np.array([[1 / w, 0, 0], [0, 1 / h, 0], [0, 0, 1]], float)
    return (Si @ np.vstack([M, [0, 0, 1]]) @ S)[:2]


def match(ref: Features, cur: Features) -> tuple[int, np.ndarray | None]:
    """Number of agreeing corners, and the transform from the ref picture to the current one (normalised)."""
    if ref.des is None or cur.des is None or len(ref.pts) < 8 or len(cur.pts) < 8:
        return 0, None
    pairs = _matcher.knnMatch(ref.des, cur.des, k=2)
    good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < RATIO * p[1].distance]
    if len(good) < 10:
        return len(good) // 2, None
    src = np.float32([ref.pts[m.queryIdx] for m in good])
    dst = np.float32([cur.pts[m.trainIdx] for m in good])
    M, inliers = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=4.0)
    if M is None:
        return 0, None
    return int(inliers.sum()), px_to_norm(M, cur.size)


def movement(A: np.ndarray) -> float:
    """How far a transform moves the middle of the picture, as a fraction of the frame, plus turn and zoom."""
    centre = A @ np.array([0.5, 0.5, 1.0])
    shift = float(np.hypot(centre[0] - 0.5, centre[1] - 0.5))
    scale = float(np.sqrt(abs(np.linalg.det(A[:, :2]))))
    angle = float(np.degrees(np.arctan2(A[1, 0], A[0, 0])))
    return shift + abs(np.log(max(scale, 1e-6))) + abs(angle) / 45.0


class RefSet:
    """Reference pictures for one tank. B maps the tank's base coordinates into each reference's."""

    def __init__(self, path: Path):
        self.path = path
        self.refs: list[tuple[Features, np.ndarray]] = []
        if path.exists():
            d = np.load(path)
            for i in range(int(d["count"])):
                f = Features(d[f"pts{i}"], d[f"des{i}"] if d[f"des{i}"].size else None,
                             tuple(int(v) for v in d[f"size{i}"]), float(d[f"bright{i}"]))
                self.refs.append((f, d[f"B{i}"]))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        arrays = {"count": np.array(len(self.refs))}
        for i, (f, B) in enumerate(self.refs):
            arrays.update({f"pts{i}": f.pts, f"des{i}": f.des if f.des is not None else np.zeros((0, 32), np.uint8),
                           f"size{i}": np.array(f.size), f"bright{i}": np.array(f.brightness), f"B{i}": B})
        tmp = self.path.with_name(self.path.stem + ".tmp.npz")
        np.savez(tmp, **arrays)
        tmp.replace(self.path)

    def best(self, cur: Features) -> tuple[int, np.ndarray | None]:
        """Best match over the references, as (agreeing corners, base -> current transform)."""
        best_n, best_A = 0, None
        for f, B in self.refs:
            n, T = match(f, cur)
            if T is not None and n > best_n:
                best_n, best_A = n, compose(T, B)
        return best_n, best_A


@dataclass
class ViewState:
    tank_id: int | None = None
    A: np.ndarray = field(default_factory=lambda: IDENTITY.copy())
    status: str = "starting"     # starting, ok, adjusted, moved_far, unknown, moving
    inliers: int = 0
    checked_ts: float = 0.0
    # True while the latest check didn't match the tank we think we're on. Events pause meanwhile, so
    # nothing gets filed against the wrong tank's boxes in the seconds before a move is confirmed.
    unsure: bool = False


class ViewTracker:
    def __init__(self, db: DB, data_dir: Path, frame_source, on_change, interval: float = 10.0):
        self.db = db
        self.dir = data_dir / "tanks"
        self.frame_source = frame_source   # () -> (frame, scores) or (None, None)
        self.on_change = on_change         # (tank_id, A, switched: bool) -> None
        self.interval = interval
        self.lock = threading.Lock()
        self.refsets: dict[int, RefSet] = {}
        self.state = ViewState()
        self.paused_until = 0.0
        self._pending_tank: tuple[int | None, int] = (None, 0)
        self._pending_A: np.ndarray | None = None
        self._misses = 0
        last = db.one("SELECT tank_id FROM views ORDER BY ts DESC LIMIT 1")
        if last and last["tank_id"] is not None:
            self.state.tank_id = last["tank_id"]
            self.state.A = self._load_align(last["tank_id"])
        self.fast_until = time.time() + 60

    # Storage

    def refset(self, tank_id: int) -> RefSet:
        if tank_id not in self.refsets:
            self.refsets[tank_id] = RefSet(self.dir / str(tank_id) / "refs.npz")
        return self.refsets[tank_id]

    def _align_path(self, tank_id: int) -> Path:
        return self.dir / str(tank_id) / "align.json"

    def _load_align(self, tank_id: int) -> np.ndarray:
        p = self._align_path(tank_id)
        return np.array(json.loads(p.read_text())) if p.exists() else IDENTITY.copy()

    def _save_align(self, tank_id: int, A: np.ndarray) -> None:
        p = self._align_path(tank_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(A.tolist()))

    # Actions from the app

    def anchor(self, tank_id: int, frame: np.ndarray) -> None:
        """Make the current picture this tank's one and only reference (setting up, or re-anchoring)."""
        with self.lock:
            rs = self.refset(tank_id)
            rs.refs = [(features(frame), IDENTITY.copy())]
            rs.save()
            self._save_align(tank_id, IDENTITY.copy())
            switched = self.state.tank_id != tank_id
            self.state = ViewState(tank_id, IDENTITY.copy(), "ok", 0, time.time(), False)
            self._set_view(tank_id)
            self._misses = 0
            self._pending_tank = (None, 0)
        self.on_change(tank_id, IDENTITY.copy(), switched)

    def forget(self, tank_id: int) -> None:
        with self.lock:
            self.refsets.pop(tank_id, None)
            for p in (self.dir / str(tank_id) / "refs.npz", self._align_path(tank_id)):
                p.unlink(missing_ok=True)
            if self.state.tank_id == tank_id:
                self.state = ViewState(None, IDENTITY.copy(), "unknown")
                self._set_view(None)
        self.on_change(None, IDENTITY.copy(), True)

    def pause(self, seconds: float) -> None:
        self.paused_until = time.time() + seconds

    def _set_view(self, tank_id: int | None) -> None:
        self.db.execute("INSERT OR REPLACE INTO views VALUES (?, ?)", (time.time(), tank_id))

    # Loop

    def start(self) -> None:
        threading.Thread(target=self._run, name="view", daemon=True).start()

    def _run(self) -> None:
        while True:
            try:
                self.check()
            except Exception:
                log.exception("view check failed")
            fast = time.time() < self.fast_until or self.state.status in ("unknown", "moving", "starting")
            time.sleep(3.0 if fast else self.interval)

    def check(self, frame: np.ndarray | None = None, scores=None) -> ViewState:
        if frame is None:
            frame, scores = self.frame_source()
        if frame is None or time.time() < self.paused_until:
            return self.state
        if scores is not None and (scores.disturbed or scores.tank > 0.3):
            self.state.status = "moving"
            self.fast_until = time.time() + 60
            return self.state

        cur = features(frame)
        results = []
        for tank_id in [r["id"] for r in self.db.query("SELECT id FROM tanks")]:
            rs = self.refset(tank_id)
            if rs.refs:
                n, A = rs.best(cur)
                results.append((n, tank_id, A))
        results.sort(key=lambda r: r[0], reverse=True)
        best = results[0] if results else (0, None, None)
        second = results[1][0] if len(results) > 1 else 0
        recognised = best[0] >= MIN_INLIERS and best[0] >= 1.5 * second

        changed = None
        with self.lock:
            st = self.state
            st.checked_ts, st.inliers = time.time(), best[0]
            st.unsure = not recognised or best[1] != st.tank_id
            if st.unsure:
                self.fast_until = time.time() + 60
            if not recognised:
                self._misses += 1
                self._pending_tank = (None, 0)
                if st.tank_id is not None and self._misses >= UNKNOWN_CHECKS:
                    log.info("view no longer recognised")
                    st.tank_id, st.A, st.status = None, IDENTITY.copy(), "unknown"
                    self._set_view(None)
                    changed = (None, st.A, True)
                elif st.tank_id is None:
                    st.status = "unknown"
            else:
                self._misses = 0
                n, tank_id, A = best
                if tank_id != st.tank_id:
                    pend_id, count = self._pending_tank
                    count = count + 1 if pend_id == tank_id else 1
                    self._pending_tank = (tank_id, count)
                    if count >= SWITCH_CHECKS or st.tank_id is None and count >= 2:
                        log.info("now looking at tank %s", tank_id)
                        st.tank_id, st.A = tank_id, A
                        st.status = "moved_far" if movement(A) > FAR_SHIFT else "ok"
                        self._save_align(tank_id, A)
                        self._set_view(tank_id)
                        self._pending_tank = (None, 0)
                        self.fast_until = time.time() + 60
                        changed = (tank_id, A, True)
                else:
                    self._pending_tank = (None, 0)
                    if np.abs(A - st.A).max() > ADJUST_EPS:
                        # Needs two checks in a row that agree, so one odd reading doesn't move the boxes.
                        if self._pending_A is not None and np.abs(A - self._pending_A).max() <= ADJUST_EPS * 2:
                            st.A = (A + self._pending_A) / 2
                            self._pending_A = None
                            self._save_align(tank_id, st.A)
                            changed = (tank_id, st.A, False)
                            log.info("camera shifted; boxes adjusted")
                        else:
                            self._pending_A = A
                            self.fast_until = time.time() + 30
                    else:
                        self._pending_A = None
                    move = movement(st.A)
                    st.status = "moved_far" if move > FAR_SHIFT else "adjusted" if move > 0.01 else "ok"
                    self._maybe_add_ref(tank_id, cur, A, n)
        if changed:
            self.on_change(*changed)
        return self.state

    def _maybe_add_ref(self, tank_id: int, cur: Features, A: np.ndarray, n: int) -> None:
        rs = self.refset(tank_id)
        if n < MIN_INLIERS * 2 or len(rs.refs) >= MAX_REFS:
            return
        if all(abs(cur.brightness - f.brightness) > NEW_REF_BRIGHTNESS for f, _ in rs.refs):
            rs.refs.append((cur, A.copy()))
            rs.save()
            log.info("tank %s: learned its look under new lighting (%d references)", tank_id, len(rs.refs))

    def status(self) -> dict:
        st = self.state
        return {"tank_id": st.tank_id, "status": st.status, "inliers": st.inliers, "checked": st.checked_ts,
                "unsure": st.unsure,
                "shift": round(movement(st.A), 3)}
