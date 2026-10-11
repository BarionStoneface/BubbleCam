import numpy as np

from bubblecam.timelapse import ease, speed_curve, windows


def test_speed_curve_endpoints():
    s = speed_curve(np.array([0.0, 1.0, 0.5]), min_speed=2, max_speed=200)
    assert s[0] == 200 and s[1] == 2
    assert abs(s[2] - 20) < 1e-9  # log scale midpoint


def test_speed_never_below_real_time():
    assert speed_curve(np.array([1.0]), min_speed=0.2, max_speed=100)[0] == 1.0


def test_ease_starts_before_activity():
    fps = 30
    ts = np.arange(0, 120, 1 / fps)
    raw = ((ts >= 60) & (ts < 65)).astype(float)
    sm = ease(ts, raw, ease_seconds=20)
    assert sm[ts < 39].max() == 0           # nothing more than one ease time before
    assert 0 < sm[(ts > 45) & (ts < 50)].max() < 1  # ramping down before he shows up
    assert sm[(ts >= 60) & (ts < 65)].min() == 1    # fully slowed while he moves
    assert 0 < sm[(ts > 75) & (ts < 84)].max() < 1  # and easing back up after


def test_windows_real_time_keeps_every_frame():
    fps = 30
    ts = np.arange(0, 10, 1 / fps)
    starts, ends = windows(ts, np.ones(len(ts)), output_fps=30, nominal_fps=fps)
    assert len(starts) == len(ts)
    assert (starts == ends).all()


def test_windows_fast_and_gap():
    fps = 30
    ts = np.concatenate([np.arange(0, 100, 1 / fps), 5000 + np.arange(0, 100, 1 / fps)])
    starts, ends = windows(ts, np.full(len(ts), 100.0), output_fps=30, nominal_fps=fps)
    # 200 s of footage at 100x = 2 s of output = 60 frames; the hour gap adds nothing.
    assert 58 <= len(starts) <= 62
    assert (np.diff(starts) > 0).all() and ends[-1] == len(ts) - 1
