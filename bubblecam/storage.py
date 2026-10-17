"""Keeping the interesting minutes and letting the boring hours go.

Three tiers:
  moments     each event is cut out of the raw footage into its own small file once it ends, and kept
              for clip_days (starred or tagged ones forever)
  timelapses  kept for timelapse_days (starred ones forever)
  raw footage takes whatever space is left; oldest deleted first when free space runs low

If space is still short after all deletable raw footage is gone, the oldest unstarred moments go next.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path

from .config import StorageConfig
from .db import DB
from .media import extract_clip
from .recorder import segment_files

log = logging.getLogger(__name__)

CLIP_DELAY = 15.0       # wait this long after an event ends so its last seconds are on disk


class Storage:
    def __init__(self, cfg: StorageConfig, db: DB, data_dir: Path, current_segment):
        self.cfg = cfg
        self.db = db
        self.data = data_dir
        self.clips_dir = data_dir / "clips"
        self.cache_dir = data_dir / "cache"
        self.current_segment = current_segment    # () -> id of the segment being written, or None
        self.pending: set[int] = set()
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.low_space = False
        recent = db.query("""SELECT id FROM events WHERE clip IS NULL AND end_ts IS NOT NULL
                             AND end_ts > ?""", (time.time() - 2 * 86400,))
        self.pending.update(r["id"] for r in recent)

    def start(self) -> None:
        threading.Thread(target=self._clip_loop, name="clips", daemon=True).start()
        threading.Thread(target=self._retention_loop, name="retention", daemon=True).start()

    # Cutting moments

    def event_closed(self, ev_id: int) -> None:
        with self.lock:
            self.pending.add(ev_id)
        self.wake.set()

    def _clip_loop(self) -> None:
        while True:
            self.wake.wait(timeout=10)
            self.wake.clear()
            try:
                self.cut_ready()
            except Exception:
                log.exception("clip cutting failed")

    def cut_ready(self, now: float | None = None) -> int:
        now = now or time.time()
        done = 0
        with self.lock:
            ids = sorted(self.pending)
        for ev_id in ids:
            ev = self.db.one("SELECT * FROM events WHERE id=?", (ev_id,))
            if not ev or ev["clip"] or ev["end_ts"] is None:
                self._drop(ev_id)
                continue
            if ev["end_ts"] + CLIP_DELAY > now:
                continue
            out = self.clips_dir / str(ev["tank_id"] or 0) / f"{ev_id}.mp4"
            if extract_clip(self.db, ev["start_ts"], ev["end_ts"], out):
                self.db.execute("UPDATE events SET clip=? WHERE id=?", (str(out), ev_id))
                done += 1
            else:
                log.warning("couldn't cut a clip for event %s (footage missing?)", ev_id)
            self._drop(ev_id)
        return done

    def _drop(self, ev_id: int) -> None:
        with self.lock:
            self.pending.discard(ev_id)

    def footage(self, start_ts: float, end_ts: float) -> Path | None:
        """A clip of any stretch of raw footage (for 'play from here'), cached briefly."""
        out = self.cache_dir / f"{int(start_ts * 10)}_{int(end_ts * 10)}.mp4"
        if out.exists() or extract_clip(self.db, start_ts, end_ts, out):
            return out
        return None

    # Retention

    def _retention_loop(self) -> None:
        while True:
            try:
                self.enforce()
            except Exception:
                log.exception("storage cleanup failed")
            time.sleep(300)

    def enforce(self, now: float | None = None) -> None:
        now = now or time.time()
        cutoff = now - self.cfg.clip_days * 86400
        for ev in self.db.query(
                """SELECT id, clip FROM events e WHERE clip IS NOT NULL AND end_ts < ? AND starred = 0
                   AND NOT EXISTS (SELECT 1 FROM event_tags t WHERE t.event_id = e.id)""", (cutoff,)):
            Path(ev["clip"]).unlink(missing_ok=True)
            self.db.execute("UPDATE events SET clip=NULL WHERE id=?", (ev["id"],))

        tl_cutoff = now - self.cfg.timelapse_days * 86400
        for tl in self.db.query("SELECT id, path FROM timelapses WHERE created_ts < ? AND starred = 0",
                                (tl_cutoff,)):
            if tl["path"]:
                Path(tl["path"]).unlink(missing_ok=True)
            self.db.execute("DELETE FROM timelapses WHERE id=?", (tl["id"],))

        if self.cache_dir.exists():
            for f in self.cache_dir.iterdir():
                if f.stat().st_mtime < now - 3600:
                    f.unlink(missing_ok=True)

        self.free_space()

    def _free_bytes(self) -> int:
        return shutil.disk_usage(self.data).free

    def free_space(self) -> None:
        need = self.cfg.min_free_gb * 1e9
        current = self.current_segment() or -1
        while self._free_bytes() < need:
            seg = self.db.one("""SELECT * FROM segments WHERE end_ts IS NOT NULL AND kept = 0 AND id != ?
                                 ORDER BY start_ts LIMIT 1""", (current,))
            if not seg:
                break
            for f in segment_files(Path(seg["path"])):
                f.unlink(missing_ok=True)
            self.db.execute("DELETE FROM segments WHERE id=?", (seg["id"],))
            log.info("deleted old raw footage %s", seg["path"])
        while self._free_bytes() < need:
            ev = self.db.one("""SELECT id, clip FROM events e WHERE clip IS NOT NULL AND starred = 0
                                AND NOT EXISTS (SELECT 1 FROM event_tags t WHERE t.event_id = e.id)
                                ORDER BY start_ts LIMIT 1""")
            if not ev:
                break
            Path(ev["clip"]).unlink(missing_ok=True)
            self.db.execute("UPDATE events SET clip=NULL WHERE id=?", (ev["id"],))
            log.warning("space is tight: deleted moment %s early", ev["id"])
        self.low_space = self._free_bytes() < need
