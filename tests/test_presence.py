import time

import numpy as np

from bubblecam.camera import SyntheticTank
from bubblecam.config import PresenceConfig
from bubblecam.db import DB
from bubblecam.presence import PresenceManager, time_out
from bubblecam.tanks import TankStore

# Cave mouth in the synthetic tank (see SyntheticTank.cave).
HOME = {"x": 0.62, "y": 0.66, "w": 0.14, "h": 0.14}


def setup(tmp_path, window=3, agree=2):
    db = DB(tmp_path / "t.db")
    store = TankStore(db)
    tank = store.create("Tank")
    spot = store.add_spotlight(tank.id, "Pleco")
    store.update_spotlight(spot.id, areas=[{"x": 0.3, "y": 0.6, "w": 0.5, "h": 0.35}], home=HOME)
    tank = store.get(tank.id)
    pm = PresenceManager(PresenceConfig(interval_seconds=0, window=window, agree=agree), db, tmp_path)
    pm.set_layout(tank, {spot.id: HOME})
    return db, store, tank, spot, pm


def test_learns_home_and_away(tmp_path):
    db, store, tank, spot, pm = setup(tmp_path)
    fake = SyntheticTank(640, 400)
    assert pm.status(spot.id)["state"] == "unknown"
    pm.add_sample(tank.id, spot.id, fake.frame(10), "home")   # pleco inside
    pm.add_sample(tank.id, spot.id, fake.frame(55), "away")   # pleco out on the sand
    assert pm.status(spot.id)["state"] == "away"

    changes = []
    pm.listeners.append(lambda t, s, state, ts: changes.append(state))
    t0 = 1000.0
    for t in np.arange(0, 120, 1.0):
        pm.update(fake.frame(t), t0 + t, settling=False)
    # in at the start of the cycle, out from 40 s, back by 70 s
    assert changes == ["home", "away", "home"]
    out_s, outings = time_out(db, spot.id, t0, t0 + 120)
    assert outings == 1 and 20 <= out_s <= 35


def test_teaching_does_not_notify(tmp_path):
    db, store, tank, spot, pm = setup(tmp_path)
    heard = []
    pm.listeners.append(lambda *a: heard.append(a))
    fake = SyntheticTank(640, 400)
    pm.add_sample(tank.id, spot.id, fake.frame(10), "home")
    pm.add_sample(tank.id, spot.id, fake.frame(55), "away")
    assert heard == []


def test_home_box_change_clears_examples(tmp_path):
    db, store, tank, spot, pm = setup(tmp_path)
    fake = SyntheticTank(640, 400)
    pm.add_sample(tank.id, spot.id, fake.frame(10), "home")
    assert pm.status(spot.id)["examples"]["home"] == 1
    store.update_spotlight(spot.id, home=dict(HOME, x=0.1))
    pm.set_layout(store.get(tank.id), {spot.id: dict(HOME, x=0.1)})
    assert pm.status(spot.id)["examples"]["home"] == 0


def test_examples_survive_restart(tmp_path):
    db, store, tank, spot, pm = setup(tmp_path)
    fake = SyntheticTank(640, 400)
    pm.add_sample(tank.id, spot.id, fake.frame(10), "home")
    pm.add_sample(tank.id, spot.id, fake.frame(55), "away")
    pm2 = PresenceManager(PresenceConfig(), db, tmp_path)
    pm2.set_layout(store.get(tank.id), {spot.id: HOME})
    assert pm2.status(spot.id)["examples"] == {"home": 1, "away": 1}
