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


def test_busy_night_is_capped_but_keeps_its_shape():
    from bubblecam.timelapse import fit_length
    fps = 30.0
    ts = np.arange(0, 10 * 3600, 1 / fps)               # ten hours
    speed = np.full(len(ts), 300.0)
    busy = (ts > 3600) & (ts < 3 * 3600)                # two hours of an active animal
    speed[busy] = 2.0
    before = np.sum(1 / fps / speed)                     # about an hour of output
    assert before > 3000
    out = fit_length(ts, speed, 300.0, 240.0, fps)
    after = np.sum(1 / fps / out)
    assert 230 < after <= 241
    assert out[busy].max() < out[~busy].min()            # busy still slower than quiet


def test_short_night_is_left_alone():
    from bubblecam.timelapse import fit_length
    ts = np.arange(0, 3600, 1 / 30)
    speed = np.full(len(ts), 300.0)
    assert np.array_equal(fit_length(ts, speed, 300.0, 240.0, 30.0), speed)
