"""Updating the app from the app: check for a newer version, pull it, restart.

Only fast forward updates: if someone changed files on the Pi by hand, nothing is overwritten and the
app says so. A change to the installer itself (new system packages and the like) can't be applied from
here; the app says when an update needs the installer run once from the Pi."""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path

from . import pi

log = logging.getLogger("bubblecam.update")
REPO = Path(__file__).resolve().parent.parent


def _git(*args: str, timeout: float = 60) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


class Updater:
    def __init__(self, background: bool = True):
        self.lock = threading.Lock()
        self.checked = 0.0
        self.info: dict = {}
        self.current = self.version()
        if background:
            threading.Thread(target=self._loop, name="update-check", daemon=True).start()

    def _loop(self) -> None:
        """Look for a new version a minute after starting, then every six hours, so the app can show
        a dot when there's one. Never installs anything by itself."""
        time.sleep(60)
        while True:
            try:
                self.check(force=True)
            except Exception:
                log.exception("update check failed")
            time.sleep(6 * 3600)

    def version(self) -> dict:
        code, out = _git("log", "-1", "--format=%h|%ct|%s")
        if code != 0 or "|" not in out:
            return {"version": None, "date": None, "subject": None}
        h, ct, subj = out.split("|", 2)
        return {"version": h, "date": int(ct), "subject": subj}

    def check(self, force: bool = False) -> dict:
        with self.lock:
            if not force and time.time() - self.checked < 6 * 3600 and self.info:
                return self.info
            info = self.version()
            code, out = _git("fetch", "--quiet", "origin", timeout=60)
            if code != 0:
                info.update(available=None, error="Couldn't reach GitHub to check. Is the Pi online?")
            else:
                _, behind = _git("rev-list", "--count", "HEAD..@{u}")
                _, log_out = _git("log", "--format=%s", "HEAD..@{u}")
                _, files = _git("diff", "--name-only", "HEAD", "@{u}")
                n = int(behind) if behind.isdigit() else 0
                info.update(available=n > 0, behind=n, changes=[l for l in log_out.splitlines() if l][:12],
                            needs_installer="deploy/install.sh" in files.split(), error=None)
            self.checked, self.info = time.time(), info
            return info

    def apply(self) -> dict:
        with self.lock:
            before = self.version()["version"]
            _, files = _git("diff", "--name-only", "HEAD", "@{u}")
            code, out = _git("pull", "--ff-only", "--quiet", timeout=120)
            if code != 0:
                log.error("update failed: %s", out)
                return {"ok": False, "error": "The update couldn't be applied: files on the Pi were changed by hand. "
                                              "Run deploy/install.sh from the Pi once."}
            after = self.version()["version"]
            self.info, self.checked = {}, 0.0
            log.info("updated %s -> %s; restarting", before, after)
            # Restart a moment after answering, so the phone gets the reply.
            threading.Timer(1.5, lambda: pi.act("restart_app")).start()
            return {"ok": True, "from": before, "to": after, "needs_installer": "deploy/install.sh" in files.split()}
