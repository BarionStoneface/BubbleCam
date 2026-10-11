"""SQLite index of recordings, events, per minute activity and timelapse renders."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
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
    kind TEXT NOT NULL,          -- 'zone' or 'burst'
    zone TEXT,
    start_ts REAL NOT NULL,
    end_ts REAL,                 -- NULL while still happening
    peak REAL DEFAULT 0,
    peak_ts REAL,
    starred INTEGER DEFAULT 0,
    thumb TEXT
);
CREATE INDEX IF NOT EXISTS events_start ON events(start_ts);

CREATE TABLE IF NOT EXISTS activity (
    minute_ts INTEGER PRIMARY KEY,
    zone_max REAL, zone_mean REAL,
    tank_max REAL, tank_mean REAL,
    zone_moving_s REAL            -- seconds in this minute the watch zones were above threshold
);

CREATE TABLE IF NOT EXISTS timelapses (
    id INTEGER PRIMARY KEY,
    path TEXT,
    created_ts REAL NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    params TEXT,
    status TEXT NOT NULL,         -- 'queued', 'rendering', 'done', 'failed'
    progress REAL DEFAULT 0,
    error TEXT
);
"""


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def execute(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, args)

    def query(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self.query(sql, args)
        return rows[0] if rows else None

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

    def open_event(self, kind: str, zone: str | None, start_ts: float) -> int:
        return self.execute("INSERT INTO events(kind, zone, start_ts) VALUES (?,?,?)",
                            (kind, zone, start_ts)).lastrowid

    def close_event(self, ev_id: int, end_ts: float, peak: float, peak_ts: float, thumb: str | None) -> None:
        self.execute("UPDATE events SET end_ts=?, peak=?, peak_ts=?, thumb=? WHERE id=?",
                     (end_ts, peak, peak_ts, thumb, ev_id))

    # Timelapses

    def add_timelapse(self, start_ts: float, end_ts: float, params: dict, created_ts: float) -> int:
        return self.execute(
            "INSERT INTO timelapses(created_ts, start_ts, end_ts, params, status) VALUES (?,?,?,?, 'queued')",
            (created_ts, start_ts, end_ts, json.dumps(params))).lastrowid
