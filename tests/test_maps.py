import numpy as np
import cv2

from bubblecam.camera import SyntheticTank
from bubblecam.maps import HeatMaps, _night_window, trails_from_frames


def test_night_window_names_night_after_its_start_date():
    import datetime as d
    ts = d.datetime(2026, 10, 12, 2, 0).timestamp()      # 2 am on the 12th belongs to the night of the 11th
    assert _night_window(ts, "19:00", "09:00") == ("night", "2026-10-11")
    ts = d.datetime(2026, 10, 12, 14, 0).timestamp()
    assert _night_window(ts, "19:00", "09:00") == ("day", "2026-10-12")
    ts = d.datetime(2026, 10, 12, 20, 0).timestamp()
    assert _night_window(ts, "19:00", "09:00") == ("night", "2026-10-12")


def test_trails_find_the_snail_not_the_school():
    fake = SyntheticTank(1280, 800)
    fake.out_from, fake.out_to = 1e9, 1e9        # pleco stays home tonight
    frames = []
    for minute in range(90):
        f = fake.frame(minute * 60.0)
        frames.append(cv2.resize(f, (480, 300), interpolation=cv2.INTER_AREA))
    img, chains = trails_from_frames(frames)
    assert img.shape[1] == 960
    assert len(chains) >= 1
    longest = max(chains, key=len)
    assert len(longest) >= 30                     # followed for half an hour or more
    # It runs left to right along the middle of the tank, like the snail does.
    ys = [p[1] for p in longest]
    assert 120 < np.mean(ys) < 180 and longest[-1][0] > longest[0][0]


def test_heatmap_accumulates_and_renders(tmp_path):
    hm = HeatMaps(tmp_path, "19:00", "09:00")
    import datetime as d
    ts = d.datetime.now().replace(hour=23).timestamp()
    mask = np.zeros((200, 320), np.uint8)
    mask[100:120, 50:90] = 1
    for i in range(50):
        hm.add(1, ts + i, mask)
    hm.save()
    assert list((tmp_path / "tanks" / "1" / "heat").glob("*_night.npy"))
    png = hm.render(1, "night", 7, np.full((800, 1280), 80, np.uint8))
    assert png and png[:4] == b"\x89PNG"
    assert hm.render(1, "day", 7, None) is None
