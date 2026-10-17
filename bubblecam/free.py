"""Free record: the camera as a plain camera.

No tank, no animals, no alerts. Press Record, press Stop. Each recording gets its own folder under
free/, named by date and time (and whatever you called it), holding the full speed video in parts,
the one frame a second proxy, and a timelapse made when the recording stops. Nothing in here is ever
cleared out to make room; delete it yourself from the Recordings screen."""

from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from .config import Config
from .db import DB
from .recorder import Recorder, read_act, act_path, proxy_paths
from .timelapse import TimelapseParams, TimelapseWorker

log = logging.getLogger("bubblecam.free")

TIMELAPSE_NAME = "timelapse.mp4"


def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")
    return s[:40]


class FreeRecord:
    def __init__(self, cfg: Config, db: DB, data: Path, width: int, height: int, fps: float,
                 timelapses: TimelapseWorker):
        self.cfg, self.db, self.data = cfg, db, data
        self.width, self.height, self.fps = width, height, fps
        self.timelapses = timelapses
        self.lock = threading.Lock()
        self.recorder: Recorder | None = None
        self.session_id: int | None = None
        self.start_ts = 0.0
        # A recording that was running when the app last stopped has no end: close it now.
        for row in db.query("SELECT * FROM free_sessions WHERE end_ts IS NULL"):
            end = db.value("SELECT MAX(COALESCE(end_ts, start_ts)) FROM segments WHERE path LIKE ?",
                           (row["dir"] + "/%",)) or row["start_ts"]
            db.execute("UPDATE free_sessions SET end_ts=? WHERE id=?", (end, row["id"]))
            self._queue_timelapse(row["id"])

    @property
    def active(self) -> bool:
        return self.recorder is not None

    def start(self, name: str = "") -> dict:
        with self.lock:
            if self.recorder:
                return self.status()
            ts = time.time()
            stamp = datetime.fromtimestamp(ts).strftime("%Y-%m-%d_%H%M")
            name = name.strip() or datetime.fromtimestamp(ts).strftime("%a %d %b, %H:%M")
            folder = self.data / "free" / (stamp + (f"_{_slug(name)}" if _slug(name) else ""))
            folder.mkdir(parents=True, exist_ok=True)
            self.session_id = self.db.execute(
                "INSERT INTO free_sessions(name, dir, start_ts) VALUES (?,?,?)",
                (name, str(folder), ts)).lastrowid
            self.start_ts = ts
            self.recorder = Recorder(self.cfg.record, folder, self.width, self.height, self.fps, self.db,
                                     flat=True, kept=True)
            log.info("free recording started: %s", folder)
            return self.status()

    def submit(self, frame: np.ndarray, ts: float, motion: float) -> None:
        rec = self.recorder
        if rec:
            rec.submit(frame, ts, motion, np.zeros(4, np.float32), -1)

    def stop(self) -> dict:
        with self.lock:
            rec, sid = self.recorder, self.session_id
            if not rec:
                return self.status()
            self.recorder = None
            threading.Thread(target=self._finish, args=(rec, sid), name="free-stop", daemon=True).start()
            return {"active": False, "session_id": sid, "finishing": True}

    def _finish(self, rec: Recorder, sid: int) -> None:
        rec.stop()
        end = self.db.value("SELECT MAX(COALESCE(end_ts, start_ts)) FROM segments WHERE path LIKE ?",
                            (str(rec.rec_dir) + "/%",)) or time.time()
        self.db.execute("UPDATE free_sessions SET end_ts=? WHERE id=?", (end, sid))
        log.info("free recording stopped: %s", rec.rec_dir)
        self._queue_timelapse(sid)

    def _queue_timelapse(self, sid: int) -> int | None:
        row = self.db.one("SELECT * FROM free_sessions WHERE id=?", (sid,))
        if not row or row["end_ts"] is None or row["end_ts"] - row["start_ts"] < 20:
            return None
        t = self.cfg.timelapse
        p = TimelapseParams(max_speed=t.max_speed, min_speed=t.min_speed, ease_seconds=t.ease_seconds,
                            driver="tank", output_fps=t.output_fps)
        tl_id = self.timelapses.queue(None, row["start_ts"], row["end_ts"] + 1, p, auto=True,
                                      out_path=str(Path(row["dir"]) / TIMELAPSE_NAME))
        self.db.execute("UPDATE free_sessions SET timelapse_id=? WHERE id=?", (tl_id, sid))
        return tl_id

    def make_timelapse(self, sid: int) -> int | None:
        row = self.db.one("SELECT * FROM free_sessions WHERE id=?", (sid,))
        if not row:
            return None
        tl = self.db.one("SELECT * FROM timelapses WHERE id=?", (row["timelapse_id"],)) if row["timelapse_id"] else None
        if tl and tl["status"] in ("queued", "rendering"):
            return tl["id"]
        return self._queue_timelapse(sid)

    def status(self) -> dict:
        rec = self.recorder
        if not rec:
            return {"active": False}
        folder = Path(rec.rec_dir)
        size = sum(f.stat().st_size for f in folder.glob("*") if f.is_file())
        return {"active": True, "session_id": self.session_id, "start_ts": self.start_ts,
                "seconds": round(time.time() - self.start_ts), "bytes": size, "dropped": rec.dropped,
                "encoder": rec.encoder}

    def sessions(self) -> list[dict]:
        out = []
        for row in self.db.query("SELECT * FROM free_sessions ORDER BY start_ts DESC"):
            folder = Path(row["dir"])
            parts = self.db.query("SELECT * FROM segments WHERE path LIKE ? ORDER BY start_ts", (row["dir"] + "/%",))
            tl = self.db.one("SELECT status, progress, path FROM timelapses WHERE id=?", (row["timelapse_id"],)) \
                if row["timelapse_id"] else None
            size = sum(f.stat().st_size for f in folder.glob("*") if f.is_file()) if folder.exists() else 0
            out.append({
                "id": row["id"], "name": row["name"], "start_ts": row["start_ts"], "end_ts": row["end_ts"],
                "live": row["id"] == self.session_id and self.recorder is not None,
                "seconds": round((row["end_ts"] or time.time()) - row["start_ts"]),
                "bytes": size, "folder": folder.name,
                "parts": [{"n": i, "start_ts": p["start_ts"], "end_ts": p["end_ts"],
                           "seconds": round((p["end_ts"] or time.time()) - p["start_ts"])}
                          for i, p in enumerate(parts)],
                "timelapse": None if not tl else tl["status"],
                "timelapse_progress": tl["progress"] if tl else 0,
            })
        return out

    def part_path(self, sid: int, n: int) -> Path | None:
        row = self.db.one("SELECT dir FROM free_sessions WHERE id=?", (sid,))
        if not row:
            return None
        parts = self.db.query("SELECT path FROM segments WHERE path LIKE ? ORDER BY start_ts", (row["dir"] + "/%",))
        if n < 0 or n >= len(parts):
            return None
        p = Path(parts[n]["path"])
        return p if p.exists() else None

    def timelapse_path(self, sid: int) -> Path | None:
        row = self.db.one("SELECT dir FROM free_sessions WHERE id=?", (sid,))
        if not row:
            return None
        p = Path(row["dir"]) / TIMELAPSE_NAME
        return p if p.exists() else None

    def rename(self, sid: int, name: str) -> None:
        self.db.execute("UPDATE free_sessions SET name=? WHERE id=?", (name.strip()[:60] or "Recording", sid))

    def delete(self, sid: int) -> bool:
        row = self.db.one("SELECT * FROM free_sessions WHERE id=?", (sid,))
        if not row:
            return False
        if row["id"] == self.session_id and self.recorder:
            self.stop()
            time.sleep(1)
        self.db.execute("DELETE FROM segments WHERE path LIKE ?", (row["dir"] + "/%",))
        if row["timelapse_id"]:
            self.db.execute("DELETE FROM timelapses WHERE id=?", (row["timelapse_id"],))
        self.db.execute("DELETE FROM free_sessions WHERE id=?", (sid,))
        folder = Path(row["dir"])
        if folder.exists() and folder.parent.name == "free":
            shutil.rmtree(folder, ignore_errors=True)
        return True
