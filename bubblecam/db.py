"""SQLite store: tanks and spotlights, recordings, events, activity, and housekeeping logs."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS tanks (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    created_ts REAL NOT NULL,
    settings TEXT NOT NULL DEFAULT '{}'      -- ignore areas, sensitivity, feeding detection...
);

-- Animals the app pays extra attention to. Areas are in the tank's reference picture coordinates.
CREATE TABLE IF NOT EXISTS spotlights (
    id INTEGER PRIMARY KEY,
    tank_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    slot INTEGER NOT NULL,                   -- 0..3: column in the per frame recording sidecar
    areas TEXT NOT NULL DEFAULT '[]',        -- where it hangs out
    home TEXT,                               -- cave mouth or perch, for in/out
    created_ts REAL NOT NULL,
    still_label TEXT NOT NULL DEFAULT 'busy in one place',   -- your words for moving without going anywhere
    moving_label TEXT NOT NULL DEFAULT 'moving around'       -- ...and for travelling
);

-- Which tank the camera was looking at, from this time on (NULL tank = didn't recognise the view).
CREATE TABLE IF NOT EXISTS views (
    ts REAL PRIMARY KEY,
    tank_id INTEGER
);

CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    start_ts REAL NOT NULL,
    end_ts REAL,
    frames INTEGER DEFAULT 0,
    fps REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS segments_start ON segments(start_ts);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    tank_id INTEGER,
    kind TEXT NOT NULL,          -- 'spot', 'burst', 'feeding', 'chase'
    spot_id INTEGER,
    detail TEXT,                 -- e.g. 'chase' or 'dart' for chase events
    start_ts REAL NOT NULL,
    end_ts REAL,                 -- NULL while still happening
    peak REAL DEFAULT 0,
    peak_ts REAL,
    thumb TEXT,
    clip TEXT,                   -- own clip file, cut once the event ends
    starred INTEGER DEFAULT 0,
    wrong INTEGER DEFAULT 0      -- marked "this was wrong"
);
CREATE INDEX IF NOT EXISTS events_tank_start ON events(tank_id, start_ts);

CREATE TABLE IF NOT EXISTS event_tags (
    event_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    PRIMARY KEY (event_id, tag)
);

CREATE TABLE IF NOT EXISTS activity (
    tank_id INTEGER NOT NULL,
    minute_ts INTEGER NOT NULL,
    tank_mean REAL, tank_max REAL,
    maintenance INTEGER DEFAULT 0,
    PRIMARY KEY (tank_id, minute_ts)
);

CREATE TABLE IF NOT EXISTS spot_activity (
    spot_id INTEGER NOT NULL,
    minute_ts INTEGER NOT NULL,
    tank_id INTEGER NOT NULL,
    moving_s REAL,               -- seconds this minute it was moving
    max REAL,
    still_s REAL DEFAULT 0,      -- of those, seconds it was busy without going anywhere (fanning, grazing)
    PRIMARY KEY (spot_id, minute_ts)
);

CREATE TABLE IF NOT EXISTS presence (
    spot_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    tank_id INTEGER NOT NULL,
    state TEXT NOT NULL,         -- 'home' or 'away'
    PRIMARY KEY (spot_id, ts)
);

CREATE TABLE IF NOT EXISTS feedings (
    id INTEGER PRIMARY KEY,
    tank_id INTEGER,
    ts REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'button',   -- 'button' or 'auto'
    event_id INTEGER
);

CREATE TABLE IF NOT EXISTS maintenance (
    id INTEGER PRIMARY KEY,
    tank_id INTEGER,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS timelapses (
    id INTEGER PRIMARY KEY,
    tank_id INTEGER,
    path TEXT,
    created_ts REAL NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    params TEXT,
    status TEXT NOT NULL,         -- 'queued', 'rendering', 'done', 'failed'
    progress REAL DEFAULT 0,
    error TEXT,
    starred INTEGER DEFAULT 0,
    auto INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS health_log (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    severity TEXT NOT NULL,       -- 'problem' or 'note'
    start_ts REAL NOT NULL,
    end_ts REAL
);
"""


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._retire_old(path)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute(f"PRAGMA user_version={VERSION}")

    @staticmethod
    def _retire_old(path: Path) -> None:
        """A database from an older layout is set aside (not deleted) and a fresh one started."""
        if not path.exists():
            return
        conn = sqlite3.connect(str(path))
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        has_tables = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        conn.close()
        if has_tables and version < VERSION:
            old = path.with_name(f"{path.stem}.v{version}.{int(time.time())}{path.suffix}")
            path.rename(old)
            for suffix in ("-wal", "-shm"):
                Path(str(path) + suffix).unlink(missing_ok=True)
            log.warning("older database moved to %s", old)

    def execute(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, args)

    def query(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def value(self, sql: str, args: tuple = ()):
        row = self.one(sql, args)
        return next(iter(row.values())) if row else None

    # Segments

    def add_segment(self, path: str, start_ts: float, fps: float) -> int:
        return self.execute("INSERT INTO segments(path, start_ts, fps) VALUES (?,?,?)",
                            (path, start_ts, fps)).lastrowid

    def close_segment(self, seg_id: int, end_ts: float, frames: int) -> None:
        self.execute("UPDATE segments SET end_ts=?, frames=? WHERE id=?", (end_ts, frames, seg_id))

    def segments_between(self, start_ts: float, end_ts: float) -> list[dict]:
        return self.query(
            "SELECT * FROM segments WHERE start_ts < ? AND (end_ts IS NULL OR end_ts > ?) ORDER BY start_ts",
            (end_ts, start_ts))

    # Events

    def open_event(self, tank_id: int | None, kind: str, spot_id: int | None, start_ts: float) -> int:
        return self.execute("INSERT INTO events(tank_id, kind, spot_id, start_ts) VALUES (?,?,?,?)",
                            (tank_id, kind, spot_id, start_ts)).lastrowid

    def close_event(self, ev_id: int, end_ts: float, peak: float, peak_ts: float, thumb: str | None) -> None:
        self.execute("UPDATE events SET end_ts=?, peak=?, peak_ts=?, thumb=? WHERE id=?",
                     (end_ts, peak, peak_ts, thumb, ev_id))

    # Timelapses

    def add_timelapse(self, tank_id: int | None, start_ts: float, end_ts: float, params: dict,
                      created_ts: float, auto: bool = False) -> int:
        return self.execute(
            """INSERT INTO timelapses(tank_id, created_ts, start_ts, end_ts, params, status, auto)
               VALUES (?,?,?,?,?, 'queued', ?)""",
            (tank_id, created_ts, start_ts, end_ts, json.dumps(params), int(auto))).lastrowid

    # Views

    def view_at(self, ts: float) -> int | None:
        row = self.one("SELECT tank_id FROM views WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (ts,))
        return row["tank_id"] if row else None

    def tank_intervals(self, tank_id: int, start_ts: float, end_ts: float) -> list[tuple[float, float]]:
        """Stretches of time in [start, end) when the camera was looking at this tank."""
        before = self.one("SELECT tank_id FROM views WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (start_ts,))
        rows = self.query("SELECT ts, tank_id FROM views WHERE ts > ? AND ts < ? ORDER BY ts", (start_ts, end_ts))
        out = []
        current = before["tank_id"] if before else None
        t = start_ts
        for r in rows:
            if current == tank_id:
                out.append((t, r["ts"]))
            current, t = r["tank_id"], r["ts"]
        if current == tank_id:
            out.append((t, end_ts))
        return out
