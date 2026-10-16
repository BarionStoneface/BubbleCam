#!/usr/bin/env bash
# BubbleCam installer for Raspberry Pi OS. Run it again any time to update; it skips what's already done.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ME="$(id -un)"
PORT=8080
MOUNT=/mnt/bubblecam

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
ask()  { local a; read -r -p "    $1 [y/n] " a </dev/tty; [[ $a == [yY]* ]]; }

if [[ $EUID -eq 0 ]]; then
  echo "Run this as your normal user (without sudo). It will ask for your password when it needs it."
  exit 1
fi

say "Installing system packages (takes a few minutes the first time)"
sudo apt-get update -qq
sudo apt-get install -y -qq git curl ffmpeg v4l-utils python3-venv python3-opencv python3-numpy >/dev/null
note "done"

say "Getting the latest BubbleCam code"
if command -v gh >/dev/null; then gh auth setup-git >/dev/null 2>&1 || true; fi
if git -C "$REPO_DIR" pull --ff-only -q 2>/dev/null; then
  note "up to date"
else
  note "couldn't reach GitHub; carrying on with the code already here"
fi

say "Setting up Python"
python3 -m venv --system-site-packages "$REPO_DIR/.venv"
"$REPO_DIR/.venv/bin/pip" install -q --upgrade fastapi uvicorn pyyaml qrcode
note "done"

say "Looking for the camera"
CAM_DEV="$(v4l2-ctl --list-devices 2>/dev/null | awk '/usb-/{usb=1; next} usb && /\/dev\/video/{print $1; exit}')"
if [[ -n "$CAM_DEV" ]]; then
  note "found a USB camera at $CAM_DEV"
else
  CAM_DEV=/dev/video0
  note "no USB camera found right now. Plug it in and run this script again,"
  note "or carry on: the app will keep retrying until the camera appears."
fi

say "Storage for footage"
DATA_DIR=""
if mountpoint -q "$MOUNT"; then
  DATA_DIR="$MOUNT/data"
  note "using the USB stick already set up at $MOUNT"
else
  mapfile -t STICKS < <(lsblk -dpno NAME,TRAN,SIZE,MODEL | awk '$2=="usb"')
  if (( ${#STICKS[@]} == 0 )); then
    note "no USB stick found. Footage will go on the SD card for now (fine for testing,"
    note "but it fills up fast). Plug in a stick and run this script again to switch."
  else
    note "USB drives found:"
    for i in "${!STICKS[@]}"; do note "  $((i + 1))) ${STICKS[$i]}"; done
    read -r -p "    Which one should hold the footage? Type its number, or press Enter to skip: " pick </dev/tty
    if [[ "$pick" =~ ^[0-9]+$ ]] && (( pick >= 1 && pick <= ${#STICKS[@]} )); then
      DEV="$(awk '{print $1}' <<<"${STICKS[$((pick - 1))]}")"
      note ""
      note "EVERYTHING on $DEV (${STICKS[$((pick - 1))]#* }) will be erased."
      read -r -p "    Type ERASE to go ahead: " confirm </dev/tty
      if [[ "$confirm" == "ERASE" ]]; then
        # The desktop may have auto-opened it.
        for part in $(lsblk -lnpo NAME "$DEV" | tail -n +2); do sudo umount "$part" 2>/dev/null || true; done
        sudo wipefs -aq "$DEV"
        echo 'type=83' | sudo sfdisk -q "$DEV"
        sudo udevadm settle
        sleep 1
        PART="$(lsblk -lnpo NAME "$DEV" | tail -n +2 | head -1)"
        sudo mkfs.ext4 -q -F -L bubblecam "$PART"
        sudo mkdir -p "$MOUNT"
        grep -q "LABEL=bubblecam" /etc/fstab || \
          echo "LABEL=bubblecam $MOUNT ext4 defaults,noatime,nofail 0 2" | sudo tee -a /etc/fstab >/dev/null
        sudo systemctl daemon-reload
        sudo mount "$MOUNT"
        DATA_DIR="$MOUNT/data"
        note "stick ready"
      else
        note "skipped; nothing was erased"
      fi
    fi
  fi
fi
if [[ -z "$DATA_DIR" ]]; then
  DATA_DIR="$HOME/bubblecam-data"
fi
sudo mkdir -p "$DATA_DIR"
sudo chown "$ME": "$DATA_DIR"
note "footage goes to $DATA_DIR"

say "Settings file"
CONFIG="$REPO_DIR/config.yaml"
if [[ ! -f "$CONFIG" ]]; then
  sed -e "s|data_dir: .*|data_dir: $DATA_DIR|" -e "s|device: .*|device: $CAM_DEV|" \
    "$REPO_DIR/config.example.yaml" > "$CONFIG"
  note "created config.yaml"
else
  sed -i "s|^  data_dir: .*|  data_dir: $DATA_DIR|" "$CONFIG"
  note "kept your config.yaml (footage folder set to $DATA_DIR)"
fi

say "Starting BubbleCam, and making it start on every boot"
sudo tee /etc/systemd/system/bubblecam.service >/dev/null <<EOF
[Unit]
Description=BubbleCam aquarium recorder
After=network-online.target local-fs.target
Wants=network-online.target

[Service]
User=$ME
WorkingDirectory=$REPO_DIR
ExecStart=$REPO_DIR/.venv/bin/python -m bubblecam --config $CONFIG
# The app checks in every 10 s; if it stops making progress for 2 minutes, systemd restarts it.
Type=notify
NotifyAccess=main
WatchdogSec=120
TimeoutStartSec=180
Restart=always
RestartSec=5
Nice=-5

[Install]
WantedBy=multi-user.target
EOF
# The app's Setup screen has restart and shut down buttons, and one that turns on phone access.
# Exactly these commands, nothing else, run without a password.
echo "$ME ALL=(root) NOPASSWD: /usr/bin/systemctl restart bubblecam, /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff, /usr/bin/tailscale serve *" | sudo tee /etc/sudoers.d/bubblecam >/dev/null
sudo chmod 440 /etc/sudoers.d/bubblecam
# If the whole Pi locks up, the hardware watchdog reboots it.
sudo mkdir -p /etc/systemd/system.conf.d
printf '[Manager]\nRuntimeWatchdogSec=15\n' | sudo tee /etc/systemd/system.conf.d/bubblecam-watchdog.conf >/dev/null
if [[ ! -e /dev/watchdog ]]; then
  BOOTCFG=/boot/firmware/config.txt
  [[ -f $BOOTCFG ]] || BOOTCFG=/boot/config.txt
  if [[ -f $BOOTCFG ]] && ! grep -q "^dtparam=watchdog=on" "$BOOTCFG"; then
    echo "dtparam=watchdog=on" | sudo tee -a "$BOOTCFG" >/dev/null
    note "hardware watchdog switched on; it takes effect after the next restart of the Pi"
  fi
fi
sudo systemctl daemon-reexec
sudo systemctl daemon-reload
sudo systemctl enable -q bubblecam
sudo systemctl restart bubblecam
for _ in $(seq 30); do
  curl -s -o /dev/null "http://localhost:$PORT/api/status" && break
  sleep 1
done
if curl -s -o /dev/null "http://localhost:$PORT/api/status"; then
  note "running"
else
  note "it didn't come up. See what went wrong with:  journalctl -u bubblecam -n 50"
fi

say "Phone access (Tailscale)"
TS_URL=""
if ! command -v tailscale >/dev/null; then
  if ask "Install Tailscale so you can watch from your phone, anywhere?"; then
    curl -fsSL https://tailscale.com/install.sh | sh
  fi
fi
if command -v tailscale >/dev/null; then
  if ! tailscale status >/dev/null 2>&1; then
    note "Tailscale will print a link. Open it on your phone or computer and log in"
    note "with the same account you use in the Tailscale phone app."
    sudo tailscale up
  fi
  note "Connecting the app to Tailscale. The first time, it may print a link to switch on HTTPS"
  note "for your account: open it, click enable, and this will carry on by itself."
  sudo tailscale serve --bg "$PORT" || note "that didn't work; run  sudo tailscale serve --bg $PORT  later"
  TS_NAME="$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
  [[ -n "$TS_NAME" ]] && TS_URL="https://$TS_NAME"
  if [[ -n "$TS_URL" ]] && ! grep -q "^notify:" "$CONFIG"; then
    printf '\nnotify:\n  public_url: %s\n' "$TS_URL" >> "$CONFIG"
    sudo systemctl restart bubblecam
  fi
fi

if [[ -d "$HOME/.config/labwc" ]] || command -v labwc >/dev/null; then
  say "Monitor"
  AUTOSTART="$HOME/.config/labwc/autostart"
  if grep -qs "kiosk.sh" "$AUTOSTART"; then
    note "live view already starts on the monitor at login"
  elif ask "Show the live view full screen on this monitor every time the Pi starts?"; then
    mkdir -p "$(dirname "$AUTOSTART")"
    echo "$REPO_DIR/deploy/kiosk.sh &" >> "$AUTOSTART"
    note "done; it starts next time you log in (the menu button in the corner has an exit)"
  fi
fi

say "All set"
note "On this Pi:     http://localhost:$PORT"
[[ -n "$TS_URL" ]] && note "On your phone:  $TS_URL   (Tailscale app switched on)"
note "To update later, run:  $REPO_DIR/deploy/install.sh"
