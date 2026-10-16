"""The Pi itself: how to reach the app from a phone, and the few things worth a button on the
monitor (leave the full screen view, restart, shut down).

Restarting and shutting down need root. The installer writes a sudoers rule allowing exactly these
commands with no password; anything else still asks."""

from __future__ import annotations

import json
import logging
import socket
import subprocess

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


def act(name: str) -> tuple[bool, str]:
    if name not in ACTIONS:
        return False, "no such action"
    log.info("pi action: %s", name)
    code, out = _run(ACTIONS[name], timeout=30)
    # pkill returns 1 when nothing matched, which is fine: there was no kiosk to leave.
    ok = code == 0 or (name == "exit_kiosk" and code == 1)
    return ok, out
