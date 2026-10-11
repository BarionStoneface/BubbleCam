import numpy as np

from bubblecam.events import Hysteresis

FRAME = np.zeros((80, 128), np.uint8)


def run(tr, pattern, fps=10):
    out = []
    for i, on in enumerate(pattern):
        c = tr.update(bool(on), i / fps, 0.5 if on else 0.0, FRAME)
        if c:
            out.append((c, i / fps))
    return out


def test_brief_pass_is_ignored():
    # a guppy crossing the zone for one second
    assert run(Hysteresis(3, 5), [0] * 10 + [1] * 10 + [0] * 100) == []


def test_shuffling_pleco_counts():
    # moves in fits: 0.8 s on, 0.5 s off, repeatedly
    pattern = ([1] * 8 + [0] * 5) * 6 + [0] * 100
    changes = run(Hysteresis(3, 5), pattern)
    assert [c for c, _ in changes] == ["start", "end"]


def test_end_waits_for_quiet():
    changes = run(Hysteresis(1, 5), [1] * 30 + [0] * 49 + [1] * 10 + [0] * 100)
    assert [c for c, _ in changes] == ["start", "end"]


def test_long_stretch_is_split():
    # Fanning all night: on continuously for 25 minutes at 1 fps becomes several moments of at most 10.
    tr = Hysteresis(3, 5, max_s=600)
    changes = run(tr, [1] * 1500, fps=1)
    starts = [t for c, t in changes if c == "start"]
    assert len(starts) == 3
