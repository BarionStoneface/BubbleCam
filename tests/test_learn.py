from bubblecam.learn import patches


def test_same_spot_becomes_one_patch_and_a_diagonal_stays_several():
    leaf = [(0.10, 0.10, 0.05, 0.05)] * 6 + [(0.11, 0.105, 0.05, 0.05)] * 4      # a leaf nodding in place
    diag = [(0.40 + i * 0.06, 0.40 + i * 0.06, 0.06, 0.06) for i in range(5)]     # something sweeping corner to corner
    samples = [[b] for b in leaf] + [[b] for b in diag for _ in range(3)]
    out = patches(samples)
    leaf_p = [p for p in out if p["x"] < 0.2]
    diag_p = [p for p in out if p["x"] >= 0.35]
    assert len(leaf_p) == 1 and leaf_p[0]["hits"] == 10
    assert len(diag_p) >= 4                                 # not one big square over the diagonal
    assert all(p["w"] < 0.2 for p in diag_p)


def test_one_off_flickers_are_dropped():
    assert patches([[(0.5, 0.5, 0.05, 0.05)]]) == []
