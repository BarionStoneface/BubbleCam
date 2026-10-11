import time
from pathlib import Path

from bubblecam.config import StorageConfig
from bubblecam.db import DB
from bubblecam.storage import Storage


def make(tmp_path, free_bytes):
    db = DB(tmp_path / "t.db")
    st = Storage(StorageConfig(min_free_gb=1, clip_days=30, timelapse_days=90), db, tmp_path, lambda: None)
    st._free_bytes = lambda: free_bytes[0]
    return db, st


def clip(db, tmp_path, ev_id, age_days, starred=0, tag=None):
    p = tmp_path / "clips" / f"{ev_id}.mp4"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    t = time.time() - age_days * 86400
    db.execute("INSERT INTO events(id, tank_id, kind, start_ts, end_ts, clip, starred) VALUES (?,1,'spot',?,?,?,?)",
               (ev_id, t, t + 30, str(p), starred))
    if tag:
        db.execute("INSERT INTO event_tags VALUES (?, ?)", (ev_id, tag))
    return p


def test_old_moments_go_but_starred_and_tagged_stay(tmp_path):
    db, st = make(tmp_path, [10e9])
    old = clip(db, tmp_path, 1, 40)
    starred = clip(db, tmp_path, 2, 40, starred=1)
    tagged = clip(db, tmp_path, 3, 40, tag="attack")
    fresh = clip(db, tmp_path, 4, 5)
    st.enforce()
    assert not old.exists() and starred.exists() and tagged.exists() and fresh.exists()
    assert db.value("SELECT clip FROM events WHERE id=1") is None
    assert db.value("SELECT COUNT(*) FROM events") == 4   # the record and thumbnail stay


def test_raw_goes_before_moments(tmp_path):
    free = [0.5e9]
    db, st = make(tmp_path, free)
    seg = tmp_path / "rec" / "a.mp4"
    seg.parent.mkdir(parents=True)
    seg.write_bytes(b"x")
    db.execute("INSERT INTO segments(path, start_ts, end_ts, fps) VALUES (?,?,?,30)", (str(seg), 1, 2))
    moment = clip(db, tmp_path, 1, 1)
    deleted = []
    real_unlink = Path.unlink

    def unlink(self, missing_ok=False):
        deleted.append(self.name)
        if self.name == "a.mp4":
            free[0] = 5e9     # deleting the raw segment frees enough
        return real_unlink(self, missing_ok=missing_ok)

    Path.unlink = unlink
    try:
        st.free_space()
    finally:
        Path.unlink = real_unlink
    assert "a.mp4" in deleted and moment.exists() and not st.low_space
