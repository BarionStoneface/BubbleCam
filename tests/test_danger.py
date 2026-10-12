import time
from types import SimpleNamespace

from bubblecam.camera import SyntheticTank
from bubblecam.config import DetectConfig
from bubblecam.danger import BubbleWatch, SurfaceWatch
from bubblecam.db import DB
from bubblecam.detector import Detector, Scores
from bubblecam.health import Health

BOX = {"x": 0.06, "y": 0.0, "w": 0.08, "h": 1.0}   # over the synthetic tank's bubble stream


def run_bubbles(stop_at, restart_at=None, seconds=600, usable=lambda t: True):
    fake = SyntheticTank(640, 400)
    det = Detector(DetectConfig(), 640, 400)
    det.set_layout({}, [BOX], BOX)       # bubbles are usually also an ignore area; still measured
    bw = BubbleWatch(ratio=0.35, stop_seconds=180)
    changes = []
    for i in range(seconds * 5):         # 5 fps is plenty
        t = i / 5
        fake.bubbles_on = not (t >= stop_at and (restart_at is None or t < restart_at))
        s = det.analyze(fake.frame(t), t)
        c = bw.update(t, s.bubbles, usable(t))
        if c:
            changes.append((c, round(t)))
    return changes


def test_bubbles_stop_and_restart():
    changes = run_bubbles(stop_at=200, restart_at=450)
    assert [c for c, _ in changes] == ["stopped", "running"]
    stopped_at = changes[0][1]
    assert 380 <= stopped_at <= 405        # 200 s, plus the 30 s window settling, plus 3 minutes
    assert 450 <= changes[1][1] <= 470


def test_running_bubbles_never_alarm():
    assert run_bubbles(stop_at=10**9) == []


def test_unusable_time_does_not_count():
    # Stopped at 200 s, but maintenance from 190 to 600: no alarm.
    assert run_bubbles(stop_at=200, usable=lambda t: not (190 <= t <= 600)) == []


def test_surface_crowding_needs_long_stretch_and_ignores_feeding():
    sw = SurfaceWatch(share=0.6, minutes=30)
    out = [sw.update(t, 0.9, 0.05, True, fed_recently=False) for t in range(0, 1900)]
    assert "start" in out and out.index("start") >= 1800
    sw2 = SurfaceWatch(share=0.6, minutes=30)
    assert all(sw2.update(t, 0.9, 0.05, True, fed_recently=True) is None for t in range(0, 4000))


def test_danger_alert_goes_out_once_and_clears(tmp_path):
    sent = []
    core = SimpleNamespace(db=DB(tmp_path / "t.db"), notifier=SimpleNamespace(
        danger_alert=lambda m, s: sent.append(m), system_alert=lambda m, s: None, state={}))
    h = Health(core)
    h.set_external("bubbles:1", True, "The filter bubbles in 20 gallon have stopped", "danger")
    h.set_external("bubbles:1", True, "again", "danger")
    assert h.status()["level"] == "danger" and len(sent) == 1
    h.set_external("bubbles:1", False)
    assert h.status()["level"] == "ok"


def test_new_lighting_has_to_be_learned_first():
    bw = BubbleWatch(ratio=0.35, stop_seconds=180)
    # Learned with the light on (bright)...
    for t in range(300):
        bw.update(t + 0.5, 0.15, True, brightness=150)
        bw.update(t + 1.0, 0.15, True, brightness=150)
    # ...then lights off and the bubbles look much fainter under IR: not a stop, just new lighting.
    out = []
    for t in range(300, 900):
        out.append(bw.update(t + 0.5, 0.04, True, brightness=50))
        out.append(bw.update(t + 1.0, 0.04, True, brightness=50))
    assert "stopped" not in out
