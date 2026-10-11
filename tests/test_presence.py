import time

import numpy as np

from bubblecam.camera import SyntheticTank
from bubblecam.config import PresenceConfig
from bubblecam.db import DB
from bubblecam.presence import Presence, time_out
from bubblecam.summary import emerged_after, morning_text, summarize

# Cave mouth in the synthetic tank (see SyntheticTank.cave).
HOME = {"name": "cave mouth", "kind": "home", "x": 0.62, "y": 0.66, "w": 0.14, "h": 0.14}


def test_learns_home_and_away(tmp_path):
    db = DB(tmp_path / "t.db")
    tank = SyntheticTank(640, 400)
    p = Presence(PresenceConfig(interval_seconds=0, window=3, agree=2), db, tmp_path)
    p.set_zones([HOME])
    assert p.status()["state"] == "unknown"
    p.add_sample(tank.frame(10), "home")   # pleco inside
    p.add_sample(tank.frame(55), "away")   # pleco out on the sand
    assert p.status()["state"] == "away"

    changes = []
    p.listeners.append(lambda s, ts: changes.append(s))
    t0 = 1000.0
    for t in np.arange(0, 120, 1.0):
        p.update(tank.frame(t), t0 + t, settling=False)
    # in at the start of the cycle, out from 40 s, back by 70 s
    assert changes == ["home", "away", "home"]
    out_s, outings = time_out(db, t0, t0 + 120)
    assert outings == 1 and 20 <= out_s <= 35


def test_zone_change_clears_examples(tmp_path):
    db = DB(tmp_path / "t.db")
    tank = SyntheticTank(640, 400)
    p = Presence(PresenceConfig(), db, tmp_path)
    p.set_zones([HOME])
    p.add_sample(tank.frame(10), "home")
    assert p.counts()["home"] == 1
    p.set_zones([dict(HOME, x=0.1)])
    assert p.counts()["home"] == 0


def test_examples_survive_restart(tmp_path):
    db = DB(tmp_path / "t.db")
    tank = SyntheticTank(640, 400)
    p = Presence(PresenceConfig(), db, tmp_path)
    p.set_zones([HOME])
    p.add_sample(tank.frame(10), "home")
    p.add_sample(tank.frame(55), "away")
    p2 = Presence(PresenceConfig(), db, tmp_path)
    p2.set_zones([HOME])
    assert p2.counts() == {"home": 1, "away": 1}


def test_emerged_after_and_summary(tmp_path):
    db = DB(tmp_path / "t.db")
    now = time.time() - 3600
    db.execute("INSERT INTO presence VALUES (?, 'home')", (now - 100,))
    db.execute("INSERT INTO feedings(ts) VALUES (?)", (now,))
    db.execute("INSERT INTO presence VALUES (?, 'away')", (now + 600,))
    db.execute("INSERT INTO presence VALUES (?, 'home')", (now + 900,))
    db.execute("INSERT INTO events(kind, zone, start_ts, end_ts, peak) VALUES ('zone','cave',?,?,0.1)",
               (now + 650, now + 800))
    db.execute("INSERT INTO activity VALUES (?, 0.1, 0.05, 0.1, 0.05, 30)", (int(now + 600) // 60 * 60,))
    assert emerged_after(db, now) == 600
    s = summarize(db, now - 1000, now + 2000)
    assert s["outings"] == 1 and abs(s["out_minutes"] - 5) < 0.01 and s["visits"] == 1
    title, body = morning_text(s)
    assert title == "Out 1 time, 5 min total"
    assert "First seen" in body
