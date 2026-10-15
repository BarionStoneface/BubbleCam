import time
from types import SimpleNamespace

from bubblecam.db import DB
from bubblecam.detector import Scores
from bubblecam.health import Health


def fake_core(tmp_path):
    alerts = []
    core = SimpleNamespace(
        db=DB(tmp_path / "t.db"), data=tmp_path, recorder=None, scores=Scores(brightness=80),
        last_frame_wall=time.time(), last_change_wall=time.time(),
        storage=SimpleNamespace(low_space=False),
        view=SimpleNamespace(state=SimpleNamespace(status="ok")),
        tanks=SimpleNamespace(all=lambda: [1]),
        notifier=SimpleNamespace(system_alert=lambda m, s: alerts.append(m), state={}),
        drop_rate=lambda: 0.0, capture_alive=lambda: True)
    return core, alerts


def test_problem_needs_to_persist_and_alerts_once(tmp_path):
    core, alerts = fake_core(tmp_path)
    h = Health(core)
    now = time.time()
    core.last_frame_wall = now - 100          # camera gone quiet
    h.check(now)
    assert h.status()["level"] == "ok"        # not yet: has to stay true for 30 s
    h.check(now + 31)
    assert h.status()["level"] == "problem" and len(alerts) == 1
    h.check(now + 60)
    assert len(alerts) == 1                   # no repeats
    core.last_frame_wall = now + 61           # camera back
    h.check(now + 62)
    assert h.status()["level"] == "ok"
    assert core.db.value("SELECT end_ts FROM health_log") is not None


def test_notes_do_not_alert(tmp_path):
    core, alerts = fake_core(tmp_path)
    core.recorder, core.drop_rate = SimpleNamespace(last_write=time.time()), lambda: 0.2
    h = Health(core)
    now = time.time()
    h.check(now)
    h.check(now + 121)
    assert h.status()["level"] == "note" and alerts == []
