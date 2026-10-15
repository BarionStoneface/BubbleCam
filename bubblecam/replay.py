"""Run footage through BubbleCam's detection offline, and score it against what really happened.

This is how the first real nights tune the app: record, mark what actually happened, replay, compare,
adjust, replay again. Kept labelled footage becomes a test set, so later changes can't quietly make
things worse.

    python -m bubblecam.replay footage.mp4 --layout layout.json [--labels labels.json] [--out report/]
    python -m bubblecam.replay /mnt/bubblecam/data/rec/2026-10-14 --layout layout.json --start 21:00 --end 06:00

footage      one video file, or a folder of BubbleCam's own recordings (its .act sidecars give the real
             timestamps; otherwise frames are timed from --fps)
layout.json  the tank as the app sees it: GET /api/tanks/<id>/layout saved to a file
labels.json  what really happened, e.g.
             [{"kind": "spot", "name": "Pleco", "start": "23:14:00", "end": "23:19:30"},
              {"kind": "feeding", "start": "19:02:00", "end": "19:03:00"},
              {"kind": "chase", "start": "01:40:12", "end": "01:40:16"},
              {"kind": "none", "start": "02:00:00", "end": "04:00:00", "note": "nothing at all here"}]
             Times are clock times on the footage's day (or seconds from the start for a plain video).

The report lists every event found, which labels it matched, what it missed, and what it made up.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

from .config import Config, load_config
from .danger import Danger
from .db import DB
from .detector import Detector
from .events import EventManager
from .presence import PresenceManager
from .recorder import act_path, read_act
from .tanks import TankStore, transform_rect
from .tracks import Behaviour


def frames_from_video(path: Path, fps: float, start_ts: float):
    cap = cv2.VideoCapture(str(path))
    i = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frame.ndim == 3:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        yield frame, start_ts + i / fps
        i += 1
    cap.release()


def frames_from_recordings(folder: Path, start_ts: float | None, end_ts: float | None):
    """BubbleCam's own segments, in order, with their real timestamps."""
    for video in sorted(folder.glob("*.mp4")):
        if ".proxy" in video.name:
            continue
        act = read_act(act_path(video))
        if not len(act):
            continue
        if end_ts and act["ts"][0] > end_ts or start_ts and act["ts"][-1] < start_ts:
            continue
        cap = cv2.VideoCapture(str(video))
        i = 0
        while True:
            ok, frame = cap.read()
            if not ok or i >= len(act):
                break
            ts = float(act["ts"][i])
            i += 1
            if start_ts and ts < start_ts or end_ts and ts > end_ts:
                continue
            if frame.ndim == 3:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            yield frame, ts
        cap.release()


class _FakeView:
    state = type("S", (), {"status": "ok"})()


class _FakeHealth:
    def __init__(self):
        self.notes = []

    def set_external(self, kind, active, message="", severity="note", since=None):
        if active and not any(n["kind"] == kind and n["end"] is None for n in self.notes):
            self.notes.append({"kind": kind, "message": message, "severity": severity, "start": since, "end": None})
        elif not active:
            for n in self.notes:
                if n["kind"] == kind and n["end"] is None:
                    n["end"] = time.time()


class _FakeNotifier:
    def __init__(self):
        self.sent = []
        self.state = {}

    def danger_alert(self, m, s):
        self.sent.append(m)

    def system_alert(self, m, s):
        self.sent.append(m)


class Replay:
    """The detection half of the app, with no camera, recorder or web."""

    def __init__(self, cfg: Config, layout: dict, width: int, height: int, workdir: Path):
        self.cfg = cfg
        self.db = DB(workdir / "replay.db")
        self.tanks = TankStore(self.db)
        tank = self.tanks.create(layout.get("name", "Replay tank"))
        settings = {k: v for k, v in layout.get("settings", {}).items() if k in ("spot_sensitivity", "burst_sensitivity", "feeding_detection", "ignore", "bubbles", "bubble_alert")}
        self.tanks.update_settings(tank.id, settings)
        self.names: dict[int, str] = {}
        for s in layout.get("spotlights", []):
            sp = self.tanks.add_spotlight(tank.id, s["name"])
            self.tanks.update_spotlight(sp.id, areas=s.get("areas", []), home=s.get("home"))
            self.names[sp.id] = s["name"]
        self.tank = self.tanks.get(tank.id)
        self.detector = Detector(cfg.detect, width, height)
        spots = {s.id: s.areas for s in self.tank.spotlights}
        self.detector.set_layout(spots, self.tank.setting("ignore"), self.tank.setting("bubbles"))
        self.events = EventManager(cfg.zone_events, cfg.burst_events, cfg.feeding, self.db, workdir / "thumbs", cfg.chase)
        self.events.set_tank(self.tank, 0)
        self.events.night_window = (cfg.timelapse.night_start, cfg.timelapse.night_end)
        self.presence = PresenceManager(cfg.presence, self.db, workdir)
        self.presence.set_layout(self.tank, {s.id: s.home for s in self.tank.spotlights if s.home})
        self.behaviour = {s.id: Behaviour() for s in self.tank.spotlights}
        self.health = _FakeHealth()
        self.notifier = _FakeNotifier()
        # Danger watches want a core; give them just what they read.
        self.latest = None
        self.last_change_wall = time.time()
        self.view = _FakeView()
        self.tank_id = tank.id
        self.danger = Danger(self, cfg.danger)
        self.frames = 0
        self.modes: dict[int, list[tuple[float, str]]] = {s.id: [] for s in self.tank.spotlights}

    def _in_maintenance(self, now):
        return False

    def feed(self, frame: np.ndarray, ts: float) -> None:
        self.latest = frame
        self.last_change_wall = time.time()
        scores = self.detector.analyze(frame, ts)
        self.events.update(scores, ts, frame)
        self.presence.update(frame, ts, scores.settling)
        self.danger.update(self.tank, ts, scores)
        for sid, b in self.behaviour.items():
            mode = b.update(ts, scores.spots.get(sid, 0) >= self.tank.zone_threshold, scores.centroids.get(sid))
            if not self.modes[sid] or self.modes[sid][-1][1] != mode:
                self.modes[sid].append((ts, mode))
        self.frames += 1

    def finish(self, ts: float) -> list[dict]:
        self.events.close_all(ts)
        evs = self.db.query("SELECT * FROM events ORDER BY start_ts")
        for e in evs:
            e["name"] = self.names.get(e["spot_id"])
        return evs


def parse_when(value, day: datetime | None, start_ts: float) -> float:
    if isinstance(value, (int, float)):
        return start_ts + float(value)
    if day is not None and ":" in str(value):
        t = datetime.strptime(value, "%H:%M:%S" if value.count(":") == 2 else "%H:%M").time()
        dt = datetime.combine(day.date(), t)
        if dt.timestamp() < start_ts - 3600:
            dt += timedelta(days=1)
        return dt.timestamp()
    return start_ts + float(value)


def score(events: list[dict], labels: list[dict], day, start_ts: float) -> dict:
    """Match found events to labels of the same kind that overlap in time."""
    truth = []
    for lab in labels:
        truth.append({**lab, "s": parse_when(lab["start"], day, start_ts), "e": parse_when(lab["end"], day, start_ts),
                      "hit": False})
    report = {"matched": [], "missed": [], "extra": [], "in_quiet": []}
    for ev in events:
        es, ee = ev["start_ts"], ev["end_ts"] or ev["start_ts"] + 1
        match = None
        for t in truth:
            same = t["kind"] == ev["kind"] and (t.get("name") is None or t.get("name") == ev.get("name"))
            if same and es < t["e"] and ee > t["s"]:
                match = t
                break
        if match:
            match["hit"] = True
            report["matched"].append((ev, match))
        else:
            quiet = any(t["kind"] == "none" and es < t["e"] and ee > t["s"] for t in truth)
            (report["in_quiet"] if quiet else report["extra"]).append(ev)
    report["missed"] = [t for t in truth if t["kind"] != "none" and not t["hit"]]
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bubblecam.replay", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("footage", help="a video file, or a folder of BubbleCam recordings")
    ap.add_argument("--layout", required=True, help="the tank's layout JSON (from /api/tanks/<id>/layout)")
    ap.add_argument("--labels", help="what really happened, as JSON")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--fps", type=float, default=30.0, help="frame rate of a plain video file")
    ap.add_argument("--start", help="clock time to begin at, for a recordings folder (HH:MM)")
    ap.add_argument("--end", help="clock time to stop at (HH:MM)")
    ap.add_argument("--every", type=int, default=1, help="analyse every Nth frame (faster, slightly rougher)")
    ap.add_argument("--out", help="folder for the report and event thumbnails")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    layout = json.loads(Path(args.layout).read_text())
    footage = Path(args.footage)
    day = None
    if footage.is_dir():
        try:
            day = datetime.strptime(footage.name, "%Y-%m-%d")
        except ValueError:
            day = datetime.now()
        start_ts = end_ts = None
        if args.start:
            start_ts = datetime.combine(day.date(), datetime.strptime(args.start, "%H:%M").time()).timestamp()
        if args.end:
            end_ts = datetime.combine(day.date(), datetime.strptime(args.end, "%H:%M").time()).timestamp()
            if start_ts and end_ts < start_ts:
                end_ts += 86400
        source = frames_from_recordings(footage, start_ts, end_ts)
    else:
        source = frames_from_video(footage, args.fps, 0.0)

    workdir = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="bubblecam-replay-"))
    workdir.mkdir(parents=True, exist_ok=True)
    rp = None
    first_ts = last_ts = None
    t0 = time.time()
    for i, (frame, ts) in enumerate(source):
        if rp is None:
            rp = Replay(cfg, layout, frame.shape[1], frame.shape[0], workdir)
            first_ts = ts
        if i % args.every:
            continue
        rp.feed(frame, ts)
        last_ts = ts
        if not args.quiet and rp.frames % 3000 == 0:
            print(f"  {rp.frames} frames, {rp.frames / (time.time() - t0):.0f} per second", file=sys.stderr)
    if rp is None:
        print("no frames found", file=sys.stderr)
        return 1
    events = rp.finish(last_ts)

    def when(ts):
        return datetime.fromtimestamp(ts).strftime("%H:%M:%S") if day else f"{ts - first_ts:7.1f}s"

    print(f"\\n{rp.frames} frames analysed, {last_ts - first_ts:.0f} s of footage, {len(events)} events found\\n")
    for e in events:
        length = (e["end_ts"] or last_ts) - e["start_ts"]
        print(f"  {when(e['start_ts'])}  {e['kind']:8s} {e['name'] or e['detail'] or '':14s} {length:6.0f} s  peak {e['peak']:.3f}")
    for sid, modes in rp.modes.items():
        stretches = [(a, b, m) for (a, m), (b, _) in zip(modes, modes[1:] + [(last_ts, None)]) if m != "quiet" and b - a >= 30]
        if stretches:
            sp = rp.tank.spotlight(sid)
            print(f"\\n{sp.name}:")
            for a, b, m in stretches:
                label = sp.still_label if m == "still" else sp.moving_label
                print(f"  {when(a)} to {when(b)}  {label}")
    if rp.health.notes:
        print("\\nWatches:")
        for n in rp.health.notes:
            print(f"  {when(n['start'] or first_ts)}  {n['message']}")

    result = {"events": events, "notes": rp.health.notes}
    if args.labels:
        labels = json.loads(Path(args.labels).read_text())
        rep = score(events, labels, day, first_ts)
        n_truth = sum(1 for l in labels if l["kind"] != "none")
        print(f"\\nAgainst the labels: {len(rep['matched'])} of {n_truth} real events found, "
              f"{len(rep['missed'])} missed, {len(rep['extra'])} extra, {len(rep['in_quiet'])} inside stretches marked as nothing")
        for t in rep["missed"]:
            print(f"  MISSED  {t['kind']} {t.get('name', '')} {t['start']} to {t['end']}")
        for e in rep["in_quiet"]:
            print(f"  FALSE   {when(e['start_ts'])} {e['kind']} {e['name'] or ''}")
        result["score"] = {k: len(v) for k, v in rep.items()}
        result["score"]["real"] = n_truth
    (workdir / "report.json").write_text(json.dumps(result, indent=2, default=str))
    print(f"\\nReport and thumbnails in {workdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
