"""Configuration: YAML file merged over dataclass defaults."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class CameraConfig:
    # "v4l2" for the real camera, "synthetic" for a fake tank (testing without hardware),
    # or a path to a video file to replay.
    source: str = "v4l2"
    device: str = "/dev/video0"
    width: int = 1280
    height: int = 800
    fps: int = 30
    # Manual exposure stops auto exposure "hunting", which looks like motion.
    # Values are in the camera's own units (check `v4l2-ctl -d /dev/video0 -l`).
    manual_exposure: bool = True
    exposure: int = 150
    gain: int = 0


@dataclass
class RecordConfig:
    enabled: bool = True
    segment_minutes: int = 10
    # "auto" tries the Pi's hardware encoder (h264_v4l2m2m) and falls back to libx264.
    encoder: str = "auto"
    bitrate: str = "1500k"


@dataclass
class DetectConfig:
    # Analysis runs on a shrunken copy of the frame; cheap enough for every frame on a Pi 4.
    analysis_width: int = 320
    analyze_every: int = 1
    # How fast the background model forgets (per analyzed frame). Lower = slow movers stand out longer.
    background_alpha: float = 0.01
    # Pixel brightness difference that counts as "changed" (0 to 255).
    diff_threshold: int = 14
    # Blobs smaller than this (in analysis pixels) are ignored: snails, shrimp antennae, sensor noise.
    min_blob_area: int = 12
    # A jump in average brightness this large means the tank light switched; reset and pause events.
    light_jump: float = 20.0
    light_settle_seconds: float = 20.0


@dataclass
class ZoneEventConfig:
    # Fraction of the zone that has to be moving to count.
    threshold: float = 0.01
    # Movement must last this long before an event starts: filters guppies passing through.
    start_seconds: float = 3.0
    # Event ends after this long with no movement.
    end_seconds: float = 15.0
    pre_roll: float = 10.0
    post_roll: float = 5.0


@dataclass
class BurstEventConfig:
    enabled: bool = True
    # Burst = tank movement this many times the rolling baseline...
    ratio: float = 3.0
    # ...and at least this much above it (fraction of the frame), so a dead quiet tank doesn't trigger on a twitch.
    floor: float = 0.02
    baseline_minutes: float = 10.0
    start_seconds: float = 1.5
    end_seconds: float = 6.0
    pre_roll: float = 8.0
    post_roll: float = 4.0


@dataclass
class TimelapseConfig:
    output_fps: int = 30
    # Speed while nothing is happening, and while the pleco is moving (1 = real time).
    max_speed: float = 300.0
    min_speed: float = 2.0
    # How long the speed takes to ease down before movement and back up after it.
    ease_seconds: float = 20.0
    # Pick the most active frame in each window once windows are at least this long (seconds of input).
    best_frame_min_window: float = 1.0
    # What drives the speed: "zone" (pleco zones), "tank" (whole tank), or "both".
    driver: str = "zone"
    # Nightly automatic render. Empty string disables.
    nightly_at: str = "09:00"
    night_start: str = "19:00"
    night_end: str = "09:00"


@dataclass
class PresenceConfig:
    # How often to check whether he's in the cave.
    interval_seconds: float = 2.0
    # Out of the last this many checks, this many must agree before the state flips.
    window: int = 5
    agree: int = 4


@dataclass
class NotifyConfig:
    # Push notifications through ntfy (free phone app). Turned on and off in the app;
    # this is only the starting value.
    enabled: bool = False
    server: str = "https://ntfy.sh"
    # Empty = a long random topic is generated on first run and shown in the app.
    topic: str = ""
    morning_at: str = "09:05"
    # Link opened when you tap a notification, e.g. your Tailscale address.
    public_url: str = ""
    on_emerge: bool = True
    emerge_cooldown_minutes: float = 60.0
    on_burst: bool = False


@dataclass
class StorageConfig:
    data_dir: str = "data"
    # Delete oldest recordings when free space drops below this.
    min_free_gb: float = 3.0


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    stream_fps: float = 8.0
    stream_width: int = 960


@dataclass
class Config:
    camera: CameraConfig = field(default_factory=CameraConfig)
    record: RecordConfig = field(default_factory=RecordConfig)
    detect: DetectConfig = field(default_factory=DetectConfig)
    zone_events: ZoneEventConfig = field(default_factory=ZoneEventConfig)
    burst_events: BurstEventConfig = field(default_factory=BurstEventConfig)
    timelapse: TimelapseConfig = field(default_factory=TimelapseConfig)
    presence: PresenceConfig = field(default_factory=PresenceConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    web: WebConfig = field(default_factory=WebConfig)

    @property
    def data_path(self) -> Path:
        return Path(self.storage.data_dir)


def _merge(obj: Any, values: dict) -> None:
    for key, value in (values or {}).items():
        if not hasattr(obj, key):
            raise ValueError(f"unknown config key: {key}")
        current = getattr(obj, key)
        if dataclasses.is_dataclass(current):
            _merge(current, value)
        else:
            setattr(obj, key, type(current)(value) if current is not None else value)


def load_config(path: str | Path | None) -> Config:
    cfg = Config()
    if path and Path(path).exists():
        with open(path) as f:
            _merge(cfg, yaml.safe_load(f) or {})
    return cfg


# Zones are edited from the web UI, so they live in their own file in the data dir
# rather than in the hand-edited config.
#
# Each zone: {"name": str, "kind": "watch" | "ignore" | "home", "x": float, "y": float, "w": float, "h": float}
# A "home" zone is the cave mouth, used to tell whether he's in or out.
# Coordinates are fractions of the frame (0 to 1), so they survive resolution changes.

def load_zones(data_dir: Path) -> list[dict]:
    p = data_dir / "zones.json"
    if not p.exists():
        return []
    return json.loads(p.read_text())


def save_zones(data_dir: Path, zones: list[dict]) -> None:
    clean = []
    for z in zones:
        kind = z.get("kind", "watch")
        if kind not in ("watch", "ignore", "home"):
            raise ValueError(f"bad zone kind: {kind}")
        if kind == "home" and any(c["kind"] == "home" for c in clean):
            raise ValueError("only one home zone (the cave mouth) is allowed")
        x, y = float(z["x"]), float(z["y"])
        w, h = float(z["w"]), float(z["h"])
        x, y = min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)
        w, h = min(max(w, 0.0), 1.0 - x), min(max(h, 0.0), 1.0 - y)
        if w <= 0 or h <= 0:
            continue
        clean.append({"name": str(z.get("name") or kind), "kind": kind, "x": x, "y": y, "w": w, "h": h})
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = data_dir / "zones.json.tmp"
    tmp.write_text(json.dumps(clean, indent=2))
    tmp.replace(data_dir / "zones.json")
