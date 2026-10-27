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


def test_busiest_box_finds_the_crowd_not_the_whole_tank():
    from bubblecam.events import busiest_box, union_box
    scattered = [(0.05, 0.6, 0.03, 0.02), (0.9, 0.7, 0.03, 0.02), (0.5, 0.85, 0.03, 0.02)]
    crowd = [(0.30 + i * 0.02, 0.05 + (i % 3) * 0.02, 0.04, 0.03) for i in range(8)]
    b = busiest_box(scattered + crowd, top_only=True)
    assert b[0] > 0.2 and b[0] + b[2] < 0.6 and b[1] + b[3] < 0.3          # around the surface crowd
    assert union_box(scattered + crowd)[2] > 0.8                            # the old way: the whole tank
