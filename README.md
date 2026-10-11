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

**Is he home?** Draw one Home box tight over the cave mouth, then teach it from the Live tab: tap "He's home" when he's in and "Cave empty" when he's out, a few times each, with the tank light on and off. It compares the cave mouth to those examples every 2 seconds and shows "In his cave" or "Out and about", with how long for.

**Fed now.** Tap it when you feed. The Activity tab then shows how long he took to come out after each feeding.

**Phone notifications** through the free ntfy app: a morning summary ("Out 3 times, 1 h 10 min total, first seen 23:14"), a "He's out" alert with a picture (at most once an hour), and optionally bursts. Switch them on in the Setup tab, which also shows the topic to subscribe to in the app.

**Activity.** A 24 hour chart of movement, time out of the cave and feedings, plus a table per night: time out of the cave, minutes moving, first and last sighting, and number of visits.

## Hardware notes

* Lock the exposure (it is locked by default). With auto exposure, the camera constantly adjusts brightness in the dark, and the detector sees that as movement.
* Point the IR lights from above or the side, not through the front glass next to the lens, or the glass reflects them straight back. A black foam ring between the lens and the glass also kills reflections.
* The OV9281 sees 850nm well. Leaving the IR on all the time gives the camera the same lighting day and night, which helps the detector.

## Install on the Pi

Open a terminal on the Pi (the black square icon at the top of the screen) and paste this one line:

```sh
sudo apt install -y gh && gh auth login -h github.com -p https -w && gh repo clone BarionStoneface/BubbleCam ~/BubbleCam && ~/BubbleCam/deploy/install.sh
```

Because the project is private on GitHub, the first part signs the Pi in: it shows a short code and opens GitHub, and you type the code there. After that, the installer:

* installs everything it needs
* finds the camera
* offers to set up the USB stick for footage (it asks first, and you have to type ERASE)
* starts BubbleCam and makes it start on every boot
* offers to set up Tailscale for your phone
* offers to show the live view on the monitor at startup

At the end it prints the address to open on your phone. To update later, run `~/BubbleCam/deploy/install.sh` again.

Useful afterwards:

```sh
journalctl -u bubblecam -f         # watch the app's log
sudo systemctl restart bubblecam   # restart after editing config.yaml
v4l2-ctl -d /dev/video0 -l         # camera controls, for tuning exposure and gain in config.yaml
```

At the default quality, real time footage takes about 16 GB a day: a 64 GB stick holds about 3 days, 128 GB about a week. Starred events are never deleted.

## First night checklist

1. Open the Setup tab. Draw a **Watch** box around his cave (and any spot he likes), a **Home** box tight over the cave mouth, and **Ignore** boxes over the bubbles and filter outflow. Save.
2. On the Live tab, tap **He's home** or **Cave empty** whenever you can see which it is. A few of each is enough to start.
3. Watch the Live tab with boxes on: green boxes are what the detector sees moving. If sensor grain makes green speckles in the dark, raise `diff_threshold`. If shrimp set off the pleco zone, raise `min_blob_area`.
4. In the morning, check Events, and the timelapse that rendered at 09:00.

## Development

```sh
pip install -r requirements.txt pytest
pytest
```

Layout: `camera.py` (sources), `detector.py` (motion), `events.py` (pleco and burst events), `presence.py` (in or out of the cave), `summary.py` (per night numbers), `notify.py` (phone notifications), `recorder.py` (real time segments and the per second side stream), `timelapse.py` (speed curve and rendering), `media.py` (reading frames back, cutting clips), `core.py` (wiring), `web/` (API and the single page UI).
