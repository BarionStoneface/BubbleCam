"""Free record: a plain camera with a Record button, recordings kept in their own folders."""

import time
from pathlib import Path

import numpy as np

from bubblecam.config import Config
from bubblecam.db import DB
from bubblecam.free import FreeRecord
from bubblecam.storage import Storage


class _Timelapses:
    def __init__(self):
        self.queued = []

    def queue(self, tank_id, start, end, params, auto=False, out_path=None):
        self.queued.append((start, end, params.driver, out_path))
        return 7


def test_free_recording_lands_in_its_own_folder_and_is_kept(tmp_path):
    cfg = Config()
    cfg.record.encoder = "libx264"
    cfg.storage.data_dir = str(tmp_path)
    db = DB(tmp_path / "t.db")
    tl = _Timelapses()
    fr = FreeRecord(cfg, db, tmp_path, 320, 200, 10.0, tl)
    st = fr.start("Moon test")
    assert st["active"] and "Moon" in Path(db.one("SELECT dir FROM free_sessions")["dir"]).name
    t0 = time.time()
    for i in range(40):
        fr.submit(np.full((200, 320), (i * 6) % 255, np.uint8), t0 + i / 10, 0.01)
    assert fr.status()["seconds"] >= 0
    fr.stop()
    for _ in range(100):
        if db.value("SELECT end_ts FROM free_sessions") is not None:
            break
        time.sleep(0.1)
    sessions = fr.sessions()
    assert len(sessions) == 1 and not sessions[0]["live"]
    s = sessions[0]
    assert s["parts"] and (tmp_path / "free" / s["folder"]).is_dir()
    assert fr.part_path(s["id"], 0).parent.name == s["folder"]
    # Short recordings get no timelapse (nothing to speed up); the segment is marked kept.
    assert db.value("SELECT kept FROM segments") == 1

    # Making room never touches it: Storage only deletes unkept footage.
    storage = Storage(cfg.storage, db, tmp_path, lambda: None)
    storage._free_bytes = lambda: 0
    storage.cfg.min_free_gb = 1
    storage.free_space()
    assert db.value("SELECT COUNT(*) FROM segments") == 1

    # Deleting from the Recordings screen removes the folder and the rows.
    assert fr.delete(s["id"])
    assert not (tmp_path / "free" / s["folder"]).exists()
    assert db.value("SELECT COUNT(*) FROM segments") == 0


def test_long_free_recording_queues_a_timelapse_in_its_folder(tmp_path):
    cfg = Config()
    cfg.storage.data_dir = str(tmp_path)
    db = DB(tmp_path / "t.db")
    tl = _Timelapses()
    fr = FreeRecord(cfg, db, tmp_path, 320, 200, 10.0, tl)
    folder = tmp_path / "free" / "x"
    folder.mkdir(parents=True)
    sid = db.execute("INSERT INTO free_sessions(name, dir, start_ts, end_ts) VALUES ('x', ?, 1000, 1600)",
                     (str(folder),)).lastrowid
    assert fr.make_timelapse(sid) == 7
    start, end, driver, out = tl.queued[0]
    assert driver == "tank" and out == str(folder / "timelapse.mp4")
    assert db.value("SELECT timelapse_id FROM free_sessions") == 7
