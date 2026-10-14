"""Configuration: YAML file merged over dataclass defaults."""

from __future__ import annotations

import dataclasses
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
    # Longer stretches are split into moments of at most this length (a shrimp fanning all night).
    max_seconds: float = 600.0


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
class FeedingConfig:
    # Feeding = a burst where most of the movement is at the surface (top quarter of the picture)...
    top_share: float = 0.55
    # ...lasting at least this long.
    start_seconds: float = 4.0
    end_seconds: float = 10.0
    cooldown_minutes: float = 90.0
    pre_roll: float = 15.0
    post_roll: float = 10.0


@dataclass
class ChaseEventConfig:
    # A dart: something crossing the tank fast while the tank is otherwise quiet (fish asleep).
    # With a second fish right behind, it's a chase.
    enabled: bool = True
    speed: float = 0.5            # frame widths per second
    min_seconds: float = 0.3
    min_path: float = 0.15        # fraction of the frame covered
    near: float = 0.09
    quiet_tank: float = 0.006     # whole tank movement must be under this (baseline over 10 min)
    cooldown: float = 20.0        # darts closer together than this are one moment
    pre_roll: float = 5.0
    post_roll: float = 4.0


@dataclass
class DangerConfig:
    # Filter bubbles: "stopped" when the bubble box shows less than this share of its usual movement...
    bubble_ratio: float = 0.35
    # ...for this long.
    bubble_stop_seconds: float = 180.0
    # Surface crowding: most movement in the top quarter of the picture for this long (not after feeding).
    surface_share: float = 0.6
    surface_minutes: float = 30.0
    # Daytime stillness: the last hour much stiller than the same hours on previous days.
    day_start: str = "10:00"
    day_end: str = "18:00"
    still_ratio: float = 0.15


@dataclass
class TimelapseConfig:
    output_fps: int = 30
    # Speed while nothing is happening, and while the pleco is moving (1 = real time).
    max_speed: float = 300.0
    min_speed: float = 2.0
    # How long the speed takes to ease down before movement and back up after it.
    ease_seconds: float = 20.0
    # What drives the speed: "spots" (spotlighted animals), "tank" (whole tank), or "both".
    driver: str = "spots"
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
    # these are only the starting values. Quiet by default: one morning summary a day,
    # plus a message if the system itself breaks.
    enabled: bool = False
    server: str = "https://ntfy.sh"
    # Empty = a long random topic is generated on first run and shown in the app.
    topic: str = ""
    morning_at: str = "09:05"
    # Link opened when you tap a notification, e.g. your Tailscale address.
    public_url: str = ""
    morning: bool = True
    system: bool = True
    danger: bool = True
    on_emerge: bool = False
    emerge_cooldown_minutes: float = 60.0
    on_burst: bool = False
    # Optional outside check in (e.g. a free Healthchecks.io check URL): pinged every few minutes while
    # all is well, so you hear about it even if the whole Pi dies. Can also be set in the app.
    checkin_url: str = ""


@dataclass
class StorageConfig:
    data_dir: str = "data"
    # Raw real time footage is deleted oldest first when free space drops below this.
    min_free_gb: float = 3.0
    # Moments (event clips) and timelapses are kept this long. Starred or tagged ones are kept for good.
    clip_days: int = 30
    timelapse_days: int = 90


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
    feeding: FeedingConfig = field(default_factory=FeedingConfig)
    chase: ChaseEventConfig = field(default_factory=ChaseEventConfig)
    danger: DangerConfig = field(default_factory=DangerConfig)
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

