"""Is BubbleCam itself OK? Separate from the fish: camera, recording, storage, heat.

Each check has to stay true for a while before it counts, so a hiccup never becomes an alert.
"problem" conditions send one phone message (if system messages are on); "note" conditions only show
in the app's status dot and the morning summary. "danger" conditions come from the tank watches in
danger.py (filter bubbles stopped) and always send a message while notifications are set up.

Two outside safety nets cover what the app can't report itself:
  systemd watchdog  restarts the app if it stops making progress
  check in URL      an outside service (e.g. Healthchecks.io) that tells you when the pings stop,
                    which also covers the whole Pi losing power or network
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class Check:
    kind: str
    severity: str        # "danger", "problem" or "note"
    persist: float       # seconds it must stay true
    message: str


CHECKS = {c.kind: c for c in [
    Check("camera", "problem", 30, "The camera isn't sending pictures"),
    Check("frozen", "problem", 120, "The camera picture is frozen"),
    Check("black", "problem", 600, "The camera picture has gone black"),
    Check("recording", "problem", 60, "Recording has stopped"),
    Check("lagging", "note", 120, "Recording is dropping frames"),
    Check("usb", "problem", 30, "The USB stick isn't connected, so footage is going to the SD card"),
    Check("full", "problem", 60, "Storage is full"),
    Check("view", "note", 60, "The camera doesn't recognise what it's looking at"),
    Check("moved", "note", 60, "The camera has moved a lot since the boxes were drawn"),
    Check("hot", "note", 300, "The Pi is running hot"),
]}

RENOTIFY_HOURS = 6
CHECKIN_EVERY = 300


def sd_notify(message: str) -> None:
    """Tell systemd we're alive (no-op when not running under systemd with a watchdog)."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(message.encode(), addr)
    except OSError:
        pass


def cpu_temp() -> float | None:
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except (OSError, ValueError):
        return None


def on_root_device(path: Path) -> bool:
    """True when a folder meant for a USB stick is actually sitting on the SD card."""
    try:
        resolved = path.resolve()
        if not str(resolved).startswith(("/mnt/", "/media/")):
            return False
        return os.stat(resolved).st_dev == os.stat("/").st_dev
    except OSError:
        return False


class Health:
    def __init__(self, core):
        self.core = core
        self.lock = threading.Lock()
        self.first_true: dict[str, float] = {}
        self.active: dict[str, dict] = {}
        self.last_notified: dict[str, float] = {}
        self.checkin_ok: bool | None = None
        self.last_checkin = 0.0
        self.extra: dict[str, str] = {}
        for row in core.db.query("SELECT * FROM health_log WHERE end_ts IS NULL"):
            # Left open by a restart: close them; they'll reopen if still true.
            core.db.execute("UPDATE health_log SET end_ts=? WHERE id=?", (time.time(), row["id"]))

    def start(self) -> None:
        sd_notify("READY=1")
        threading.Thread(target=self._run, name="health", daemon=True).start()

    def _run(self) -> None:
        while True:
            try:
                self.check()
                self.core.danger.periodic(time.time())
                if self._alive():
                    sd_notify("WATCHDOG=1")
                self._checkin()
            except Exception:
                log.exception("health check failed")
            time.sleep(10)

    def _alive(self) -> bool:
        """For the systemd watchdog: is the app making progress? (A missing camera is not a hang.)"""
        c = self.core
        if not c.capture_alive():
            return False
        if c.recorder and time.time() - c.last_frame_wall < 30:
            return time.time() - c.recorder.last_write < 120
        return True

    def truths(self, now: float) -> dict[str, bool]:
        c = self.core
        frames_flowing = now - c.last_frame_wall < 15
        t = {
            "camera": not frames_flowing,
            "frozen": frames_flowing and now - c.last_change_wall > 60,
            "black": frames_flowing and c.scores.brightness < 6,
            "recording": bool(c.recorder) and frames_flowing and now - c.recorder.last_write > 30,
            "lagging": bool(c.recorder) and c.drop_rate() > 0.05,
            "usb": on_root_device(c.data),
            "full": c.storage.low_space,
            "view": c.view.state.status == "unknown" and bool(c.tanks.all()),
            "moved": c.view.state.status == "moved_far",
        }
        temp = cpu_temp()
        t["hot"] = temp is not None and temp > 80
        self.extra["hot"] = f" ({temp:.0f}°C)" if temp else ""
        return t

    def check(self, now: float | None = None) -> None:
        now = now or time.time()
        truths = self.truths(now)
        with self.lock:
            for kind, true in truths.items():
                spec = CHECKS[kind]
                if true:
                    self.first_true.setdefault(kind, now)
                    if kind not in self.active and now - self.first_true[kind] >= spec.persist:
                        self._raise(kind, spec.message + self.extra.get(kind, ""), spec.severity,
                                    self.first_true[kind], now)
                else:
                    self.first_true.pop(kind, None)
                    if kind in self.active:
                        self._resolve(kind, now)

    def set_external(self, kind: str, active: bool, message: str = "", severity: str = "note",
                     since: float | None = None) -> None:
        """For watches that do their own timing (danger.py): switch a condition on or off right away."""
        now = time.time()
        with self.lock:
            if active and kind not in self.active:
                self._raise(kind, message, severity, since or now, now)
            elif not active and kind in self.active:
                self._resolve(kind, now)

    def _raise(self, kind: str, message: str, severity: str, since: float, now: float) -> None:
        log_id = self.core.db.execute(
            "INSERT INTO health_log(kind, message, severity, start_ts) VALUES (?,?,?,?)",
            (kind, message, severity, since)).lastrowid
        self.active[kind] = {"kind": kind, "message": message, "severity": severity,
                             "since": since, "log_id": log_id}
        log.warning("health: %s", message)
        quiet_for = (0.5 if severity == "danger" else RENOTIFY_HOURS) * 3600
        if severity in ("problem", "danger") and now - self.last_notified.get(kind, 0) > quiet_for:
            self.last_notified[kind] = now
            if severity == "danger":
                self.core.notifier.danger_alert(message, since)
            else:
                self.core.notifier.system_alert(message, since)

    def _resolve(self, kind: str, now: float) -> None:
        item = self.active.pop(kind)
        self.core.db.execute("UPDATE health_log SET end_ts=? WHERE id=?", (now, item["log_id"]))
        log.info("health: resolved %s", kind)

    def status(self) -> dict:
        rank = {"danger": 0, "problem": 1, "note": 2}
        with self.lock:
            items = sorted(self.active.values(), key=lambda i: (rank[i["severity"]], i["since"]))
        level = items[0]["severity"] if items else "ok"
        return {"level": level, "items": [{k: i[k] for k in ("kind", "message", "severity", "since")}
                                          for i in items],
                "checkin": self.checkin_ok}

    def _checkin(self) -> None:
        url = self.core.notifier.state.get("checkin_url") or ""
        if not url or time.time() - self.last_checkin < CHECKIN_EVERY:
            return
        self.last_checkin = time.time()
        # Only ping while the essentials work, so silence means something is wrong.
        if any(i["severity"] == "problem" for i in self.active.values()):
            return   # (a tank danger isn't a system fault: keep pinging so silence still means "Pi down")
        try:
            urllib.request.urlopen(url, timeout=15).read()
            self.checkin_ok = True
        except Exception as e:
            self.checkin_ok = False
            log.warning("check in ping failed: %s", e)
