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


def test_follows_small_moves_and_switches_by_hand(tmp_path):
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

    # Pointed at the other tank: nothing changes until told.
    for t in range(10, 15):
        vt.check(b.frame(t))
    assert vt.state.tank_id == ta.id
    vt.anchor(tb.id, b.frame(15))
    assert vt.state.tank_id == tb.id
    assert any(c[0] == tb.id and c[2] for c in changes)
    assert db.view_at(9e12) == tb.id


def test_stays_put_when_it_cannot_tell(tmp_path):
    """A view it doesn't know is not a reason to drop the tank: the picture may be dark or busy, or the
    camera is being carried. It keeps recording against the tank it had, and follows it back."""
    db, store, vt, changes = make(tmp_path)
    a = SyntheticTank(1280, 800, seed=1)
    ta = store.create("A")
    vt.anchor(ta.id, a.frame(1))
    stranger = SyntheticTank(1280, 800, seed=99)
    for t in range(10):
        vt.check(stranger.frame(t))
    assert vt.state.tank_id == ta.id
    assert len(changes) == 1                         # only the anchor; nothing switched since
    for t in range(3):
        vt.check(a.frame(20 + t))
    assert vt.state.tank_id == ta.id and vt.state.status == "ok"


def test_learns_new_lighting(tmp_path):
    db, store, vt, changes = make(tmp_path)
    a = SyntheticTank(1280, 800, seed=1)
    ta = store.create("A")
    vt.anchor(ta.id, a.frame(1))
    dark = (a.frame(5) * 0.45).astype(np.uint8)
    vt.check(dark)
    assert len(vt.refset(ta.id).refs) == 2


def test_lights_changing_is_not_a_move(tmp_path):
    """A brightness jump that makes the picture unrecognisable, with no shaking, means new lighting."""
    from types import SimpleNamespace
    db, store, vt, changes = make(tmp_path)
    a = SyntheticTank(1280, 800, seed=1)
    ta = store.create("A")
    vt.anchor(ta.id, a.frame(1))
    # Lights switch: the picture inverts (as different as it gets), with a light change flagged once.
    inverted = lambda t: (255 - a.frame(t)).astype(np.uint8)
    vt.check(inverted(5), SimpleNamespace(light_change=True, disturbed=False, tank=0.0))
    for t in range(6, 20):
        vt.check(inverted(t), SimpleNamespace(light_change=False, disturbed=False, tank=0.0))
    assert vt.state.tank_id == ta.id
    assert len(vt.refset(ta.id).refs) == 2
    # Later, under the new lighting, it recognises the tank outright, and still under the old.
    vt.state.inliers = 0
    vt.check(inverted(30))
    assert vt.state.inliers >= 25
    vt.check(a.frame(31))
    assert vt.state.tank_id == ta.id


def test_never_drops_the_tank(tmp_path):
    db, store, vt, changes = make(tmp_path)
    a, stranger = SyntheticTank(1280, 800, seed=1), SyntheticTank(1280, 800, seed=99)
    ta = store.create("Only tank")
    vt.anchor(ta.id, a.frame(1))
    for t in range(20):
        vt.check(stranger.frame(t))
    assert vt.state.tank_id == ta.id and vt.state.status != "moving"


def test_switching_tanks_is_manual(tmp_path):
    db, store, vt, changes = make(tmp_path)
    a, b = SyntheticTank(1280, 800, seed=1), SyntheticTank(1280, 800, seed=99)
    ta, tb = store.create("A"), store.create("B")
    vt.anchor(tb.id, b.frame(1))
    vt.anchor(ta.id, a.frame(2))
    for t in range(5, 20):
        vt.check(b.frame(t))
    assert vt.state.tank_id == ta.id                 # sat on B a while: still A until told
    vt.anchor(tb.id, b.frame(20))                    # the tap on the Tanks screen
    assert vt.state.tank_id == tb.id and changes[-1][2]
