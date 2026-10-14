import numpy as np

from bubblecam.camera import SyntheticTank
from bubblecam.config import BurstEventConfig, DetectConfig, FeedingConfig, ZoneEventConfig
from bubblecam.db import DB
from bubblecam.detector import Detector
from bubblecam.events import EventManager
from bubblecam.tanks import TankStore


def run(tmp_path, feeds, seconds=200, paused=lambda t: False):
    db = DB(tmp_path / "t.db")
    store = TankStore(db)
    tank = store.create("T")
    store.update_settings(tank.id, {"ignore": [{"x": 0.04, "y": 0, "w": 0.12, "h": 1}]})
    tank = store.get(tank.id)
    fake = SyntheticTank(640, 400)
    fake.feeds = feeds
    det = Detector(DetectConfig(), 640, 400)
    det.set_layout({}, tank.setting("ignore"))
    em = EventManager(ZoneEventConfig(), BurstEventConfig(), FeedingConfig(), db, tmp_path / "thumbs")
    em.set_tank(tank, 0)
    for i in range(seconds * 10):          # 10 fps is plenty for this
        t = i / 10
        frame = fake.frame(t)
        em.update(det.analyze(frame, t), t, frame, paused=paused(t))
    em.close_all(seconds)
    return db


def test_feeding_rush_is_one_feeding_event(tmp_path):
    db = run(tmp_path, feeds=[(120, 150)])
    kinds = [r["kind"] for r in db.query("SELECT kind FROM events ORDER BY start_ts")]
    assert kinds.count("feeding") == 1
    assert "burst" not in kinds
    assert db.value("SELECT COUNT(*) FROM feedings WHERE source='auto'") == 1


def test_quiet_tank_has_no_feeding(tmp_path):
    db = run(tmp_path, feeds=[])
    assert db.value("SELECT COUNT(*) FROM events") == 0


def test_maintenance_makes_no_events(tmp_path):
    db = run(tmp_path, feeds=[(120, 150)], paused=lambda t: 100 <= t <= 190)
    assert db.value("SELECT COUNT(*) FROM events") == 0


def test_passing_school_does_not_trigger_shrimp_spotlight(tmp_path):
    """The school keeps crossing the shrimp's perch area in the daytime; the shrimp itself is idle."""
    from bubblecam.tanks import TankStore
    db = DB(tmp_path / "t.db")
    store = TankStore(db)
    tank = store.create("T")
    sp = store.add_spotlight(tank.id, "Shrimp")
    store.update_spotlight(sp.id, areas=[{"x": 0.3, "y": 0.15, "w": 0.4, "h": 0.3}])   # right in the school's lane
    tank = store.get(tank.id)
    fake = SyntheticTank(640, 400)
    fake.out_from = fake.out_to = 1e9
    det = Detector(DetectConfig(), 640, 400)
    det.set_layout({sp.id: tank.spotlight(sp.id).areas}, [{"x": 0.04, "y": 0, "w": 0.12, "h": 1}])
    em = EventManager(ZoneEventConfig(), BurstEventConfig(), FeedingConfig(), db, tmp_path / "thumbs")
    em.set_tank(tank, 0)
    em.set_areas({sp.id: tank.spotlight(sp.id).areas})
    for i in range(120 * 15):
        t = i / 15
        frame = fake.frame(t)
        em.update(det.analyze(frame, t), t, frame)
    em.close_all(120)
    assert db.value("SELECT COUNT(*) FROM events WHERE kind='spot'") == 0
