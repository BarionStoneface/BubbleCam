"""The Pi itself: how to reach the app from a phone, and the few things worth a button on the
monitor (leave the full screen view, restart, shut down).

Restarting and shutting down need root. The installer writes a sudoers rule allowing exactly these
commands with no password; anything else still asks."""

from __future__ import annotations

import json
import logging
import shutil
import socket
import subprocess
from pathlib import Path

log = logging.getLogger("bubblecam.pi")

ACTIONS = {
    "exit_kiosk": ["pkill", "-f", "--", "--kiosk"],
    "restart_app": ["sudo", "-n", "systemctl", "restart", "bubblecam"],
    "reboot": ["sudo", "-n", "systemctl", "reboot"],
    "shutdown": ["sudo", "-n", "systemctl", "poweroff"],
}


def _run(cmd: list[str], timeout: float = 15) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def lan_ip() -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def tailscale(port: int) -> dict:
    """What a phone needs: the address, the account it must be signed into, and whether the app is
    being served on it yet."""
    info = {"installed": False, "online": False, "account": None, "url": None, "serving": False, "lan_url": None}
    ip = lan_ip()
    if ip:
        info["lan_url"] = f"http://{ip}:{port}"
    code, out = _run(["tailscale", "status", "--json"])
    if code == 127:
        return info
    info["installed"] = True
    try:
        st = json.loads(out)
    except ValueError:
        return info
    me = st.get("Self") or {}
    info["online"] = st.get("BackendState") == "Running"
    name = (me.get("DNSName") or "").rstrip(".")
    if name:
        info["url"] = f"https://{name}"
    user = (st.get("User") or {}).get(str(me.get("UserID")))
    if user:
        info["account"] = user.get("LoginName") or user.get("DisplayName")
    code, out = _run(["tailscale", "serve", "status"])
    info["serving"] = code == 0 and str(port) in out
    return info


def serve(port: int) -> tuple[bool, str]:
    code, out = _run(["sudo", "-n", "tailscale", "serve", "--bg", str(port)], timeout=60)
    return code == 0, out


def qr_svg(text: str) -> str | None:
    try:
        import qrcode
        import qrcode.image.svg as svg
    except ImportError:
        return None
    img = qrcode.make(text, image_factory=svg.SvgPathImage, box_size=10, border=2)
    return img.to_string(encoding="unicode")


def install_launchers(repo: Path, home: Path | None = None) -> list[str]:
    """Icons for the Pi's own screen: on the desktop and in the menu, one for a normal window and one
    for full screen. The screen at login opens the normal window (maximised) rather than full screen.
    Only on a Pi with a desktop; rewritten only when something changed. Returns what it wrote."""
    home = home or Path.home()
    if not (home / ".config" / "labwc").exists() and not shutil.which("chromium") and not shutil.which("chromium-browser"):
        return []
    opener = repo / "deploy" / "open.sh"
    icon = repo / "bubblecam" / "web" / "static" / "icon-512.png"
    entries = {
        "bubblecam.desktop": ("BubbleCam", "Watch the tank", f"{opener}"),
        "bubblecam-fullscreen.desktop": ("BubbleCam full screen", "Watch the tank, full screen", f"{opener} full"),
    }
    written = []
    targets = [home / ".local" / "share" / "applications"]
    if (home / "Desktop").is_dir():
        targets.append(home / "Desktop")
    for folder in targets:
        folder.mkdir(parents=True, exist_ok=True)
        for fname, (name, comment, exe) in entries.items():
            text = ("[Desktop Entry]\nType=Application\n"
                    f"Name={name}\nComment={comment}\nExec={exe}\nIcon={icon}\n"
                    "Terminal=false\nCategories=AudioVideo;Video;\nStartupWMClass=BubbleCam\n")
            path = folder / fname
            if not path.exists() or path.read_text() != text:
                path.write_text(text)
                path.chmod(0o755)
                written.append(str(path))
    autostart = home / ".config" / "labwc" / "autostart"
    if autostart.exists():
        lines = autostart.read_text().splitlines()
        new = [f"{opener} &" if "deploy/kiosk.sh" in l else l for l in lines]
        if new != lines:
            autostart.write_text("\n".join(new) + "\n")
            written.append(str(autostart))
    return written


def act(name: str) -> tuple[bool, str]:
    if name not in ACTIONS:
        return False, "no such action"
    log.info("pi action: %s", name)
    code, out = _run(ACTIONS[name], timeout=30)
    # pkill returns 1 when nothing matched, which is fine: there was no kiosk to leave.
    ok = code == 0 or (name == "exit_kiosk" and code == 1)
    return ok, out
