# BubbleCam

A night camera recorder for an aquarium, built around one shy pleco. It runs on a Raspberry Pi 4 with an Arducam OV9281 global shutter USB camera and IR lights, and you watch it on your phone over Tailscale or on a monitor plugged into the Pi.

## What it does

**Records everything in real time.** Footage is saved continuously in 10 minute segments, so nothing is ever missed. When the disk gets full, the oldest footage is deleted first, but any event you star is kept.

**Pleco zones.** You draw boxes on the picture: watch boxes around his cave and favourite spots, and ignore boxes over the bubbles, filter outflow and swaying plants. Movement in a watch box only becomes an event if it keeps going for a few seconds (3 by default). That filters out guppies and rummynose swimming through, while a pleco shuffling in fits and starts still counts. Each event becomes a clip with 10 seconds of footage before it began.

**Whole tank bursts.** It keeps a rolling baseline of how busy the tank normally is over the last 10 minutes. A burst is when movement jumps to 3 times that level, for example at feeding time or during a chase. Because the baseline moves with the tank, daytime schooling doesn't trigger it constantly and a dead quiet night doesn't trigger on every twitch.

**Dynamic timelapse.** This plays fast through empty hours and eases down toward real time whenever he moves:

1. Every recorded frame has a motion score saved next to it.
2. The speed for each moment comes from that score, smoothed so it starts slowing about 20 seconds before he appears and speeds back up gently afterward.
3. Each output frame stands in for a window of real footage. At 300x, one output frame covers 10 seconds. Out of each window, the app keeps the frame with the most movement, not whichever frame happened to land on the tick. That's how a 3 second dart out of the cave survives a fast timelapse.
4. While recording, the app also saves the single most active frame from every second to a small side stream. Quiet hours are built from that side stream, so the Pi only fully decodes footage around the moments that matter.

Quiet speed, movement speed, ease time and what triggers the slowdown (pleco zones, whole tank, or either) are all sliders in the app. Last night renders automatically every morning, and you can render any time range yourself.

**Lights.** A sudden brightness jump (the tank light switching) resets the motion detector and pauses events for 20 seconds, so you don't get a flood of false events.

**Activity.** A 24 hour chart, plus a table per night: how many minutes he spent moving, when he first came out, when he was last seen, and how many visits.

## Hardware notes

* Lock the exposure (it is locked by default). With auto exposure, the camera constantly adjusts brightness in the dark, and the detector sees that as movement.
* Point the IR lights from above or the side, not through the front glass next to the lens, or the glass reflects them straight back. A black foam ring between the lens and the glass also kills reflections.
* The OV9281 sees 850nm well. Leaving the IR on all the time gives the camera the same lighting day and night, which helps the detector.

## Install on the Pi

Raspberry Pi OS Bookworm, 64 bit.

```sh
sudo apt update
sudo apt install -y git ffmpeg v4l-utils python3-venv python3-opencv python3-numpy
git clone https://github.com/BarionStoneface/BubbleCam.git ~/BubbleCam
cd ~/BubbleCam
python3 -m venv --system-site-packages .venv
.venv/bin/pip install fastapi uvicorn pyyaml
cp config.example.yaml config.yaml
```

### USB stick for footage

Format it as ext4 (this erases it; check the device name with `lsblk` first):

```sh
lsblk
sudo mkfs.ext4 -L bubblecam /dev/sda1
sudo mkdir -p /mnt/bubblecam
echo 'LABEL=bubblecam /mnt/bubblecam ext4 defaults,noatime,nofail 0 2' | sudo tee -a /etc/fstab
sudo mount -a
sudo mkdir -p /mnt/bubblecam/data && sudo chown $USER /mnt/bubblecam/data
```

At the default bitrate, real time footage takes about 16 GB a day, so a 64 GB stick holds about 3 days and a 128 GB stick about a week.

### Try it

```sh
.venv/bin/python -m bubblecam --config config.yaml              # real camera
.venv/bin/python -m bubblecam --config config.yaml --synthetic  # fake tank, no camera needed
```

Check the camera's exposure controls and their ranges with `v4l2-ctl -d /dev/video0 -l`, then tune `exposure` and `gain` in `config.yaml`.

### Run at boot

Edit `deploy/bubblecam.service` if your username isn't `pi`, then:

```sh
sudo cp deploy/bubblecam.service /etc/systemd/system/
sudo systemctl enable --now bubblecam
journalctl -u bubblecam -f    # logs
```

### Phone access through Tailscale

```sh
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale serve --bg 8080
```

`tailscale serve` prints an https address like `https://raspberrypi.your-tailnet.ts.net`. Open it on your phone (with Tailscale running on the phone) and use "Add to Home Screen" so it opens like an app. Only devices on your own tailnet can reach it.

### Monitor on the Pi

On Raspberry Pi OS desktop, add the kiosk script to autostart:

```sh
echo "$HOME/BubbleCam/deploy/kiosk.sh &" >> ~/.config/labwc/autostart
```

## First night checklist

1. Open the Zones tab, draw a **Watch** box around his cave (and any spot he likes), and **Ignore** boxes over the bubbles and filter outflow. Save.
2. Watch the Live tab with boxes on: green boxes are what the detector sees moving. If sensor grain makes green speckles in the dark, raise `diff_threshold`. If shrimp set off the pleco zone, raise `min_blob_area`.
3. In the morning, check Events, and the timelapse that rendered at 09:00.

## Development

```sh
pip install -r requirements.txt pytest
pytest
```

Layout: `camera.py` (sources), `detector.py` (motion), `events.py` (pleco and burst events), `recorder.py` (real time segments and the per second side stream), `timelapse.py` (speed curve and rendering), `media.py` (reading frames back, cutting clips), `core.py` (wiring), `web/` (API and the single page UI).
