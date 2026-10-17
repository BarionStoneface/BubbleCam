"""Phone notifications through ntfy. Quiet by default: one morning summary a day, plus a message if
BubbleCam itself breaks. "Out and about" and burst messages exist but are off unless you turn them on.

ntfy is a free app (Android and iPhone). You subscribe to a topic name and anything posted to that
topic shows up as a notification. The topic is a long random name, so nobody else will stumble on it.
"""

from __future__ import annotations

import json
import logging
import queue
import secrets
import threading
import time
import urllib.request
from base64 import b64encode
from datetime import datetime
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .config import Config
from .db import DB

log = logging.getLogger(__name__)

TOGGLES = ("enabled", "morning", "system", "danger", "on_emerge", "on_burst")


def _header(value: str) -> str:
    """HTTP headers must be plain ASCII; ntfy decodes RFC 2047 for anything else."""
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + b64encode(value.encode()).decode() + "?="


class Notifier:
    def __init__(self, cfg: Config, db: DB, data_dir: Path, frame_source: Callable[[], np.ndarray | None],
                 morning: Callable[[], tuple[str, str, str | None]]):
        self.cfg = cfg
        self.ncfg = cfg.notify
        self.db = db
        self.frame_source = frame_source
        self.morning = morning      # () -> (title, body, image path or None)
        self.path = data_dir / "notify.json"
        self.state = self._load()
        self.q: queue.Queue = queue.Queue(maxsize=50)
        self.last_emerge: dict[int, float] = {}
        self.last_burst = 0.0
        self.last_error: str | None = None
        self.last_sent: float | None = None
        threading.Thread(target=self._sender, name="notify", daemon=True).start()
        threading.Thread(target=self._morning_loop, name="morning", daemon=True).start()

    # Settings

    def _load(self) -> dict:
        state = json.loads(self.path.read_text()) if self.path.exists() else {}
        n = self.ncfg
        for key in TOGGLES:
            state.setdefault(key, getattr(n, key, True))
        state.setdefault("topic", n.topic or f"bubblecam-{secrets.token_hex(12)}")
        if n.topic:
            state["topic"] = n.topic
        state.setdefault("checkin_url", n.checkin_url)
        # Start counting from tomorrow, so turning it on in the afternoon doesn't fire a stale summary.
        state.setdefault("last_morning", datetime.now().strftime("%Y-%m-%d"))
        self._save(state)
        return state

    def _save(self, state: dict | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state or self.state, indent=2))
        tmp.replace(self.path)

    def settings(self) -> dict:
        s = {k: self.state[k] for k in TOGGLES}
        s.update(topic=self.state["topic"], server=self.ncfg.server, morning_at=self.ncfg.morning_at,
                 checkin_url=self.state.get("checkin_url", ""), last_error=self.last_error,
                 last_sent=self.last_sent)
        return s

    def update(self, changes: dict) -> dict:
        for k in TOGGLES:
            if k in changes:
                self.state[k] = bool(changes[k])
        if "checkin_url" in changes:
            url = str(changes["checkin_url"] or "").strip()
            if url and not url.startswith(("https://", "http://")):
                raise ValueError("the check in address should start with https://")
            self.state["checkin_url"] = url
        self._save()
        return self.settings()

    # Sending

    def send(self, title: str, message: str, image: bytes | None = None, tags: str = "fish",
             force: bool = False, priority: str | None = None) -> None:
        if not (self.state["enabled"] or force):
            return
        try:
            self.q.put_nowait((title, message, image, tags, priority))
        except queue.Full:
            log.warning("notification queue full; dropping %r", title)

    def _sender(self) -> None:
        while True:
            title, message, image, tags, priority = self.q.get()
            url = f"{self.ncfg.server.rstrip('/')}/{self.state['topic']}"
            headers = {"Title": _header(title), "Tags": tags}
            if priority:
                headers["Priority"] = priority
            if self.ncfg.public_url:
                headers["Click"] = self.ncfg.public_url
            if image:
                headers.update({"Message": _header(message), "Filename": "bubblecam.jpg"})
                req = urllib.request.Request(url, data=image, headers=headers, method="PUT")
            else:
                req = urllib.request.Request(url, data=message.encode(), headers=headers, method="POST")
            for attempt in range(3):
                try:
                    urllib.request.urlopen(req, timeout=20).read()
                    self.last_error = None
                    self.last_sent = time.time()
                    break
                except Exception as e:
                    self.last_error = f"{type(e).__name__}: {e}"
                    log.warning("notification failed (%s), attempt %d", e, attempt + 1)
                    time.sleep(5 * (attempt + 1))

    def _snapshot(self) -> bytes | None:
        frame = self.frame_source()
        if frame is None:
            return None
        w = 640
        small = cv2.resize(frame, (w, round(frame.shape[0] * w / frame.shape[1])), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return buf.tobytes() if ok else None

    # Triggers

    def system_alert(self, message: str, since: float) -> None:
        if not self.state["system"]:
            return
        self.send(message, f"Since {datetime.fromtimestamp(since).strftime('%H:%M')}.", tags="warning",
                  priority="high")

    def danger_alert(self, message: str, since: float) -> None:
        """Something dangerous in a tank. Sent whatever the quiet settings, unless danger alerts are off."""
        if not self.state["danger"]:
            return
        self.send(message, f"Since {datetime.fromtimestamp(since).strftime('%H:%M')}.", self._snapshot(),
                  tags="rotating_light", priority="urgent")

    def animal_out(self, spot_id: int, name: str, ts: float) -> None:
        if not self.state["on_emerge"]:
            return
        if ts - self.last_emerge.get(spot_id, 0) < self.ncfg.emerge_cooldown_minutes * 60:
            return
        self.last_emerge[spot_id] = ts
        self.send(f"{name} is out", f"Spotted at {datetime.fromtimestamp(ts).strftime('%H:%M')}.",
                  self._snapshot(), tags="eyes")

    def burst(self, tank_name: str, ts: float) -> None:
        if not self.state["on_burst"] or ts - self.last_burst < 1800:
            return
        self.last_burst = ts
        self.send(f"Burst of activity in {tank_name}", f"At {datetime.fromtimestamp(ts).strftime('%H:%M')}.",
                  self._snapshot(), tags="zap")

    def test(self) -> None:
        self.send("BubbleCam test", "Notifications are working.", self._snapshot(), force=True)

    def morning_summary(self, force: bool = False) -> None:
        title, body, image_path = self.morning()
        if not body:
            return
        image = Path(image_path).read_bytes() if image_path and Path(image_path).exists() else None
        self.send(title, body, image, tags="sunrise", force=force)

    def _morning_loop(self) -> None:
        while True:
            try:
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                if (self.state["morning"] and self.state["last_morning"] != today
                        and now.strftime("%H:%M") >= self.ncfg.morning_at):
                    self.state["last_morning"] = today
                    self._save()
                    self.morning_summary()
            except Exception:
                log.exception("morning summary failed")
            time.sleep(30)
