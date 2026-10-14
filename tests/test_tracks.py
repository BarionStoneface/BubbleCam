import numpy as np

from bubblecam.camera import SyntheticTank
from bubblecam.config import DetectConfig
from bubblecam.danger import StillObjectWatch
from bubblecam.detector import Detector
from bubblecam.tracks import Behaviour, ChaseConfig, ChaseWatch, Tracker


def test_tracker_follows_a_moving_blob():
    tr = Tracker()
    ids = set()
    for i in range(30):
        t = i / 30
        tracks = tr.update([(0.1 + 0.02 * i, 0.5, 0.02, 0.01, 0.0002)], t)
        ids.add(tracks[0].id)
    assert len(ids) == 1
    assert abs(tracks[0].speed() - 0.6) < 0.1          # 0.02 per frame at 30 fps


def synth_blobs(fake, t, det):
    return det.analyze(fake.frame(t), t).blobs


def test_chase_in_quiet_night_tank():
    fake = SyntheticTank(1280, 800)
    fake.night = True
    fake.chases = [(60, 63)]
    det = Detector(DetectConfig(), 1280, 800)
    det.set_layout({}, [{"x": 0.04, "y": 0, "w": 0.12, "h": 1}])
    cw = ChaseWatch(ChaseConfig())
    found = []
    for i in range(80 * 30):
        t = i / 30
        s = det.analyze(fake.frame(t), t)
        r = cw.update(s.blobs, t, 0.001, quiet_ok=not s.settling)
        if r:
            found.append((r[0], round(t, 1)))
    assert len(found) == 1
    assert found[0][0] == "chase" and 60.3 <= found[0][1] <= 61.3


def test_no_chase_when_tank_is_busy():
    fake = SyntheticTank(1280, 800)          # daytime: the school is zooming about
    fake.chases = [(20, 23)]
    det = Detector(DetectConfig(), 1280, 800)
    det.set_layout({}, [{"x": 0.04, "y": 0, "w": 0.12, "h": 1}])
    cw = ChaseWatch(ChaseConfig())
    found = [cw.update(det.analyze(fake.frame(i / 30), i / 30).blobs, i / 30, 0.02, True) for i in range(30 * 30)]
    assert not any(found)


def test_behaviour_still_vs_moving():
    b = Behaviour(window=8.0, travel=0.03)
    # Fanning: active, centre jitters around one spot.
    modes = [b.update(t / 10, True, (0.8 + 0.004 * np.sin(t), 0.2 + 0.004 * np.cos(t))) for t in range(120)]
    assert modes[-1] == "still"
    b2 = Behaviour(window=8.0, travel=0.03)
    modes = [b2.update(t / 10, True, (0.5 + 0.003 * t, 0.5)) for t in range(120)]
    assert modes[-1] == "moving"
    assert b2.distance > 0.3
    assert b2.update(13.0, False, None) == "quiet"


def test_shrimp_fanning_is_still_and_foraging_is_moving():
    fake = SyntheticTank(1280, 800)
    det = Detector(DetectConfig(), 1280, 800)
    perch = {"x": 0.72, "y": 0.1, "w": 0.24, "h": 0.25}
    det.set_layout({1: [perch]}, [{"x": 0.04, "y": 0, "w": 0.12, "h": 1}])
    results = {}
    for mode in ("fanning", "foraging"):
        fake.shrimp_mode = mode
        det.restart(0, 0)
        b = Behaviour()
        last = None
        for i in range(40 * 10):
            t = i / 10
            s = det.analyze(fake.frame(t), t)
            last = b.update(t, s.spots.get(1, 0) >= 0.004, s.centroids.get(1))
        results[mode] = last
    assert results == {"fanning": "still", "foraging": "moving"}


def test_still_object_appears_then_clears():
    fake = SyntheticTank(1280, 800)
    so = StillObjectWatch()
    t = 0.0
    # Two hours of normal tank, then a fish floats dead at the surface.
    while t < 7200:
        so.add(t, fake.frame(t), 100.0, True)
        t += 120
    assert so.check(t) is None
    fake.dead_from = t
    while t < 7200 + 50 * 60:
        so.add(t, fake.frame(t), 100.0, True)
        t += 120
    found = so.check(t)
    assert found and found["surface"]
    # Three hours later it's part of "before" as well: no longer "new".
    while t < 7200 + 50 * 60 + 3 * 3600:
        so.add(t, fake.frame(t), 100.0, True)
        t += 120
        so.check(t)
    assert so.check(t + 1) is None
