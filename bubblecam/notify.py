"""Phone notifications through ntfy: a morning summary, "he's out", and optionally bursts.

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
from .summary import morning_text, night_bounds, summarize

log = logging.getLogger(__name__)

SETTINGS = ("enabled", "on_emerge", "on_burst", "morning")


def _header(value: str) -> str:
    """HTTP headers must be plain ASCII; ntfy decodes RFC 2047 for anything else."""
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + b64encode(value.encode()).decode() + "?="


class Notifier:
    def __init__(self, cfg: Config, db: DB, data_dir: Path, frame_source: Callable[[], np.ndarray | None]):
        self.cfg = cfg
        self.ncfg = cfg.notify
        self.db = db
        self.frame_source = frame_source
        self.path = data_dir / "notify.json"
        self.state = self._load()
        self.q: queue.Queue = queue.Queue(maxsize=50)
        self.last_emerge = 0.0
        self.last_burst = 0.0
        self.last_error: str | None = None
        threading.Thread(target=self._sender, name="notify", daemon=True).start()
        threading.Thread(target=self._morning_loop, name="morning", daemon=True).start()

    # Settings

    def _load(self) -> dict:
        state = {}
        if self.path.exists():
            state = json.loads(self.path.read_text())
        n = self.ncfg
        state.setdefault("enabled", n.enabled)
        state.setdefault("on_emerge", n.on_emerge)
        state.setdefault("on_burst", n.on_burst)
        state.setdefault("morning", True)
        state.setdefault("topic", n.topic or f"bubblecam-{secrets.token_hex(12)}")
        if n.topic:
            state["topic"] = n.topic
        # Start counting from tomorrow, so turning it on in the afternoon doesn't fire a stale summary.
        state.setdefault("last_morning", datetime.now().strftime("%Y-%m-%d"))
        self._save(state)
        return state

    def _save(self, state: dict | None = None) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state or self.state, indent=2))
        tmp.replace(self.path)

    def settings(self) -> dict:
        s = {k: self.state[k] for k in SETTINGS}
        s.update(topic=self.state["topic"], server=self.ncfg.server, morning_at=self.ncfg.morning_at,
                 last_error=self.last_error)
        return s

    def update(self, changes: dict) -> dict:
        for k in SETTINGS:
            if k in changes:
                self.state[k] = bool(changes[k])
        self._save()
        return self.settings()

    # Sending

    def send(self, title: str, message: str, image: bytes | None = None, tags: str = "fish",
             force: bool = False) -> None:
        if not (self.state["enabled"] or force):
            return
        try:
            self.q.put_nowait((title, message, image, tags))
        except queue.Full:
            log.warning("notification queue full; dropping %r", title)

    def _sender(self) -> None:
        while True:
            title, message, image, tags = self.q.get()
            url = f"{self.ncfg.server.rstrip('/')}/{self.state['topic']}"
            headers = {"Title": _header(title), "Tags": tags}
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

    def he_is_out(self, ts: float) -> None:
        if not self.state["on_emerge"]:
            return
        if ts - self.last_emerge < self.ncfg.emerge_cooldown_minutes * 60:
            return
        self.last_emerge = ts
        self.send("He's out", f"Spotted at {datetime.fromtimestamp(ts).strftime('%H:%M')}.", self._snapshot(),
                  tags="eyes")

    def burst(self, ts: float) -> None:
        if not self.state["on_burst"] or ts - self.last_burst < 1800:
            return
        self.last_burst = ts
        self.send("Burst of activity", f"Whole tank kicked off at {datetime.fromtimestamp(ts).strftime('%H:%M')}.",
                  self._snapshot(), tags="zap")

    def test(self) -> None:
        self.send("BubbleCam test", "Notifications are working.", self._snapshot(), force=True)

    def morning_summary(self, force: bool = False) -> None:
        t = self.cfg.timelapse
        start, end = night_bounds(datetime.now(), t.night_start, t.night_end)
        s = summarize(self.db, start, end)
        title, body = morning_text(s)
        image = None
        if s["best_thumb"] and Path(s["best_thumb"]).exists():
            image = Path(s["best_thumb"]).read_bytes()
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
