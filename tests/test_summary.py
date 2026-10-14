import time

from bubblecam.db import DB
from bubblecam.summary import emerged_after, morning_text, tank_summary
from bubblecam.tanks import TankStore


def test_night_summary_and_text(tmp_path):
    db = DB(tmp_path / "t.db")
    store = TankStore(db)
    tank = store.create("20 gallon")
    pleco = store.add_spotlight(tank.id, "Pleco")
    store.update_spotlight(pleco.id, areas=[{"x": 0.3, "y": 0.6, "w": 0.4, "h": 0.3}],
                           home={"x": 0.6, "y": 0.6, "w": 0.1, "h": 0.1})
    shrimp = store.add_spotlight(tank.id, "Shrimp")
    tank = store.get(tank.id)
    start = time.time() - 6 * 3600
    end = start + 5 * 3600
    db.execute("INSERT INTO views VALUES (?, ?)", (start - 10, tank.id))
    db.execute("INSERT INTO presence VALUES (?, ?, ?, 'home')", (pleco.id, start - 100, tank.id))
    db.execute("INSERT INTO feedings(tank_id, ts, source) VALUES (?,?, 'button')", (tank.id, start + 600))
    db.execute("INSERT INTO presence VALUES (?, ?, ?, 'away')", (pleco.id, start + 1800, tank.id))
    db.execute("INSERT INTO presence VALUES (?, ?, ?, 'home')", (pleco.id, start + 1800 + 2700, tank.id))
    for m in range(5):
        db.execute("INSERT INTO spot_activity VALUES (?,?,?,?,?,0)", (shrimp.id, int(start) + 3600 + m * 60, tank.id, 40, 0.1))
    db.execute("""INSERT INTO health_log(kind, message, severity, start_ts, end_ts)
                  VALUES ('camera', 'The camera isn''t sending pictures', 'problem', ?, ?)""",
               (start + 7200, start + 7800))

    s = tank_summary(db, tank, start, end)
    assert s["spots"][0]["outings"] == 1 and abs(s["spots"][0]["out_minutes"] - 45) < 0.1
    assert abs(s["spots"][1]["moving_minutes"] - 40 * 5 / 60) < 0.01
    assert emerged_after(db, tank, start + 600)["name"] == "Pleco"

    from bubblecam.summary import incidents
    title, body, image = morning_text([s], incidents(db, start, end))
    assert title.startswith("Pleco came out 1 time, 45 min in all")
    assert "Shrimp was moving for 3 min" in body
    assert "isn't sending pictures" in body
    # Reports, never advises.
    for word in ("should", "worth", "check on", "consider"):
        assert word not in body.lower()


def test_no_footage_text():
    title, body, image = morning_text([], [])
    assert title == "No footage last night"
