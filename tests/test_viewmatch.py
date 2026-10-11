import cv2
import numpy as np

from bubblecam.camera import SyntheticTank
from bubblecam.db import DB
from bubblecam.tanks import TankStore
from bubblecam.viewmatch import ViewTracker


def shifted(img, dx, dy):
    M = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(img, M, (img.shape[1], img.shape[0]), borderMode=cv2.BORDER_REFLECT)


def make(tmp_path):
    db = DB(tmp_path / "t.db")
    store = TankStore(db)
    changes = []
    vt = ViewTracker(db, tmp_path, lambda: (None, None), lambda *a: changes.append(a))
    return db, store, vt, changes


def test_recognises_tanks_and_follows_small_moves(tmp_path):
    db, store, vt, changes = make(tmp_path)
    a, b = SyntheticTank(1280, 800, seed=1), SyntheticTank(1280, 800, seed=7)
    ta, tb = store.create("A"), store.create("B")
    vt.anchor(tb.id, b.frame(1))
    vt.anchor(ta.id, a.frame(1))
    assert vt.state.tank_id == ta.id

    # Bumped 40 px right: boxes follow after two agreeing checks.
    for t in (5, 6, 7):
        vt.check(shifted(a.frame(t), 40, 0))
    assert vt.state.tank_id == ta.id
    assert abs(vt.state.A[0, 2] - 40 / 1280) < 0.004

    # Camera moved to the other tank: switches after a few checks.
    for t in range(10, 15):
        vt.check(b.frame(t))
    assert vt.state.tank_id == tb.id
    assert any(c[0] == tb.id and c[2] for c in changes)
    assert db.view_at(9e12) == tb.id


def test_unknown_view(tmp_path):
    db, store, vt, changes = make(tmp_path)
    a = SyntheticTank(1280, 800, seed=1)
    ta = store.create("A")
    vt.anchor(ta.id, a.frame(1))
    stranger = SyntheticTank(1280, 800, seed=99)
    for t in range(10):
        vt.check(stranger.frame(t))
    assert vt.state.tank_id is None and vt.state.status == "unknown"
    # Back in place: recognised again.
    for t in range(3):
        vt.check(a.frame(20 + t))
    assert vt.state.tank_id == ta.id


def test_learns_new_lighting(tmp_path):
    db, store, vt, changes = make(tmp_path)
    a = SyntheticTank(1280, 800, seed=1)
    ta = store.create("A")
    vt.anchor(ta.id, a.frame(1))
    dark = (a.frame(5) * 0.45).astype(np.uint8)
    vt.check(dark)
    assert len(vt.refset(ta.id).refs) == 2


def test_unsure_while_mismatched(tmp_path):
    db, store, vt, changes = make(tmp_path)
    a, b = SyntheticTank(1280, 800, seed=1), SyntheticTank(1280, 800, seed=99)
    ta = store.create("A")
    vt.anchor(ta.id, a.frame(1))
    vt.check(b.frame(5))
    assert vt.state.tank_id == ta.id and vt.state.unsure      # not switched yet, but events pause
    vt.check(a.frame(6))
    assert not vt.state.unsure
