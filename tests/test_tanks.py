import numpy as np
import pytest

from bubblecam.db import DB
from bubblecam.tanks import IDENTITY, TankStore, invert, transform_rect


def test_transform_rect_shift_and_back():
    A = np.array([[1, 0, 0.05], [0, 1, -0.02]], float)
    r = {"x": 0.2, "y": 0.3, "w": 0.1, "h": 0.1}
    moved = transform_rect(r, A)
    assert abs(moved["x"] - 0.25) < 1e-9 and abs(moved["y"] - 0.28) < 1e-9
    back = transform_rect(moved, invert(A))
    assert all(abs(back[k] - r[k]) < 1e-9 for k in "xywh")


def test_transform_clips_to_frame():
    r = transform_rect({"x": 0.9, "y": 0.9, "w": 0.2, "h": 0.2}, IDENTITY)
    assert r["x"] + r["w"] <= 1 and r["y"] + r["h"] <= 1


def test_spotlight_slots_and_limit(tmp_path):
    store = TankStore(DB(tmp_path / "t.db"))
    tank = store.create("Pleco tank")
    spots = [store.add_spotlight(tank.id, n) for n in ("Pleco", "Shrimp", "Snail", "Pirate")]
    assert sorted(s.slot for s in spots) == [0, 1, 2, 3]
    with pytest.raises(ValueError):
        store.add_spotlight(tank.id, "Fifth")
    store.delete_spotlight(spots[1].id)
    again = store.add_spotlight(tank.id, "Shrimp again")
    assert again.slot == 1   # freed slot is reused


def test_sensitivity_settings(tmp_path):
    store = TankStore(DB(tmp_path / "t.db"))
    tank = store.create("T")
    normal = tank.zone_threshold
    store.update_settings(tank.id, {"spot_sensitivity": 1})
    assert store.get(tank.id).zone_threshold > normal
    with pytest.raises(ValueError):
        store.update_settings(tank.id, {"spot_sensitivity": 9})
