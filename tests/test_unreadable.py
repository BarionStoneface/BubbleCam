"""The Pi's hardware encoder can write full speed files nothing can read. Clips and timelapses must
still come out, from the one frame a second proxy, and the recorder must notice and switch."""

import time
from pathlib import Path

import numpy as np

from bubblecam.config import Config
from bubblecam.db import DB
from bubblecam.media import extract_clip, _readable
from bubblecam.recorder import FRAG, Recorder, detect_encoder, verify_mp4
from bubblecam.timelapse import TimelapseParams, render


def _record(tmp_path, seconds=12, fps=10):
    cfg = Config()
    cfg.record.encoder = "libx264"
    cfg.storage.data_dir = str(tmp_path)
    db = DB(tmp_path / "t.db")
    rec = Recorder(cfg.record, tmp_path / "rec", 320, 200, fps, db)
    t0 = time.time() - seconds - 5
    for i in range(seconds * fps):
        frame = np.full((200, 320), 30, np.uint8)
        frame[80:120, (i * 3) % 280:(i * 3) % 280 + 40] = 220
        while rec.q.full():
            time.sleep(0.01)
        rec.submit(frame, t0 + i / fps, 0.05 if i % 30 < 10 else 0.001, np.zeros(4, np.float32), 1)
    rec.stop()
    return cfg, db, t0


def _wreck(path: Path):
    # What the hardware encoder leaves: the bytes are there but the header describing them isn't.
    data = bytearray(path.read_bytes())
    i = data.find(b"avcC")
    assert i > 0
    data[i:i + 4] = b"xxxx"
    path.write_bytes(bytes(data))
    _readable.clear()


def test_clip_and_timelapse_survive_unreadable_footage(tmp_path):
    cfg, db, t0 = _record(tmp_path)
    seg = db.one("SELECT * FROM segments")
    main = Path(seg["path"])
    assert verify_mp4(main)
    _wreck(main)
    assert not verify_mp4(main)

    out = tmp_path / "clip.mp4"
    assert extract_clip(db, t0 + 2, t0 + 9, out)
    assert verify_mp4(out)

    tl = db.add_timelapse(None, t0, t0 + 13, TimelapseParams(driver="tank", max_speed=4, min_speed=2).__dict__, time.time())
    render(db, cfg, tl)
    row = db.one("SELECT * FROM timelapses WHERE id=?", (tl,))
    assert row["status"] == "done", row["error"]
    assert verify_mp4(Path(row["path"]))


def test_recorder_switches_encoder_when_a_part_is_unreadable(tmp_path):
    cfg, db, t0 = _record(tmp_path, seconds=3)
    rec = Recorder(cfg.record, tmp_path / "rec2", 320, 200, 10, db)
    rec.encoder, rec.movflags = "h264_v4l2m2m", FRAG
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"\x00" * 1000)
    rec._check(bad)
    assert rec.unreadable == 1 and rec.encoder == "libx264"
    rec.stop()


def test_encoder_detection_checks_the_real_file():
    enc, flags = detect_encoder("auto", 320, 200, 10)
    assert enc in ("h264_v4l2m2m", "libx264") and "frag_keyframe" in flags
