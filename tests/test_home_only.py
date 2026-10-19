"""An animal with only a home box drawn must still be watched, and a timelapse whose animal boxes saw
nothing all night must not race through at top speed (what happened with Cheeks)."""

import time
from types import SimpleNamespace

import numpy as np

from bubblecam.config import Config
from bubblecam.detector import Detector
from bubblecam.tanks import Spotlight, watch_areas
from bubblecam.timelapse import interest


def test_home_box_alone_is_watched():
    cheeks = Spotlight(1, 1, "Cheeks", 0, [], {"x": 0.4, "y": 0.6, "w": 0.1, "h": 0.1})
    areas = watch_areas(cheeks)
    assert len(areas) == 1
    a = areas[0]
    assert a["x"] < 0.4 and a["x"] + a["w"] > 0.5 and a["y"] < 0.6     # home plus a margin around it

    cfg = Config()
    d = Detector(cfg.detect, 1280, 800)
    d.set_layout({1: areas}, [])
    still = np.full((800, 1280), 60, np.uint8)
    t = time.time()
    for i in range(40):
        d.analyze(still, t + i / 15)
    d.settle_until = 0
    moving = still.copy()
    moving[500:560, 540:600] = 200                  # something moves at the cave mouth
    s = d.analyze(moving, t + 3)
    assert s.spots.get(1, 0) > 0.01


def test_timelapse_falls_back_to_whole_tank_when_boxes_saw_nothing():
    from bubblecam import timelapse
    n = 3600
    spot = np.zeros(n)                                # the animal's boxes: nothing, all night
    tank = np.zeros(n)
    tank[1000:1100] = 0.05                            # but something happened in the tank
    raw = interest(spot, tank, "spots", 0.01, 0.02)
    assert raw.max() == 0
    # The same rule plan() applies:
    if float(np.mean(raw > 0.2)) < 0.002:
        raw = np.maximum(raw, interest(spot, tank, "tank", 0.01, 0.02))
    assert raw[1050] > 0.5 and raw[10] == 0
    assert "whole tank" in open(timelapse.__file__).read()
