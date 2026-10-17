# BubbleCam

A camera recorder for aquariums, built for a family rather than tech people. It started with one shy nocturnal pleco and a bamboo shrimp. It runs on a Raspberry Pi 4 with an Arducam OV9281 global shutter USB camera and IR lights, and you watch it on your phones, a tablet, or a monitor plugged into the Pi, privately over Tailscale.

The app lives entirely on the Pi. Phones and tablets just open a page the Pi serves; nothing is copied to them unless you download a clip.

## The screens

* **Tanks:** one card per tank, with a live or last seen picture and a plain line about each animal you're watching ("Pleco: home for 2 h").
* **A tank:** the live picture, your spotlighted animals and what they're doing, **Fed them** and **Doing maintenance** buttons, last night's summary with its timelapse and best moments, and the **day bar**: a noon to noon timeline with a lane per animal plus one for the whole tank. Tap or drag anywhere on it to see that moment, and play from there.
* **Moments:** every clip, filtered with plain buttons: each animal, feeding, bursts, at night, kept, your own tags. Each moment can be starred, tagged, marked "this was wrong", downloaded or deleted.
* **Timelapses:** each tank's night, ready every morning, plus "make one" for any stretch of time.
* **Trends:** nightly charts per animal and per tank, and the feeding log. Charts only: the app reports, it never advises.
* **Status dot** (top right): green when all's well; otherwise it says what's wrong in plain words.
* **Setup** (gear): tanks, who to watch, drawing boxes, notifications. **Advanced** inside it holds sensitivity, false alarm counts, storage, camera details and system history.

## What it does

**Two modes, one switch.** *Tank watch* is everything below. *Free record* makes the camera a plain camera: a live picture, a Record button, and a timelapse of each recording made when it stops. Recordings go in their own folders under `free/`, named by date and whatever you called them, and are never cleared out to make room. No tank, no animals, no alerts in that mode. The switch sits in the header on every screen.

**The Pi's own screen.** With a monitor plugged in, the Pi shows a dashboard: the live picture large, the clock, what the tank is doing right now, the latest moments, the Record button in Free record, and system status. Tap the picture for full screen; the ⋯ button in the corner exits to the desktop or shuts the Pi down.

**Tanks, and a camera that moves.** Each tank keeps its own boxes, animals, settings and history. Move the camera to the other tank and tap "The camera is on this one" on the Tanks screen; it never switches by itself and never asks. If the camera gets bumped, it measures how far by matching distinctive points in the rocks, wood and gravel against reference pictures, and moves the boxes to match.

**Spotlight: who to watch.** Pick up to four animals per tank. Draw where each one hangs out, and its home if it has one (a cave mouth, a perch). Each gets its own moments, its own lane on the day bar, its own line in the morning summary, and its own timelapse option. Movement in an animal's area only counts once it lasts a few seconds, so a guppy passing through doesn't count but a pleco shuffling in fits and starts does. Spotlight knows animals by where they live and how they move, not by face, so it suits homebodies.

**Home or out.** For an animal with a home box, tap it on the tank page and answer "home" or "out" a few times, with the light on and off. It compares the home box against those examples every 2 seconds.

**Bursts and feeding.** A burst is the whole tank suddenly getting much busier than its usual level over the last 10 minutes. When most of that rush is at the surface, it's logged as **feeding** instead, and matched with a "Fed them" tap if there was one. The feeding log shows how long it took for each animal to show up afterward.

**Maintenance.** Tap it before a water change. Nothing counts as an event and the time is left out of trends.

**This was wrong.** Any moment can be marked wrong. It then drops out of summaries and counts, and Advanced shows how often each kind of moment is wrong, which is the guide for the per tank sensitivity sliders.

**Dynamic timelapse.** It plays fast through empty hours and eases down toward real time when something moves:

1. Every recorded frame has its motion scores saved next to it.
2. The speed comes from those scores, smoothed so it starts slowing about 20 seconds before the animal appears and speeds back up gently afterward.
3. Each output frame stands in for a window of real footage. At 300x, one output frame covers 10 seconds. Out of each window the app keeps the frame with the most movement, so a 3 second dart out of the cave survives a fast timelapse.
4. While recording, the app also keeps the single most active frame from every second in a small side stream. Quiet hours are built from that, so the Pi only fully decodes footage around the moments that matter.

**Storage that keeps the good bits.** Raw footage is recorded continuously and the oldest goes first when space runs low. Each moment is cut into its own small file when it ends and kept 30 days; timelapses are kept 90 days. Anything starred or tagged is kept for good.

**Chases.** At night, with the tank quiet, something suddenly tearing across it is a dart; with a second fish right behind, a chase. These become moments (with their own filter) and a line in the morning summary ("2 chases while the tank was quiet"), never an alert. Daytime zooming doesn't count: the tank has to be quiet by the clock and by its own movement.

**Busy tanks.** A spotlighted animal's area only counts movement from slow movers. Each moving thing is followed for a couple of seconds; a pleco or shrimp covers well under a twentieth of the tank a second even when busy, a cruising guppy more. So fish passing through the pleco's area aren't the pleco. The same tracking gives each animal a reading of what it's doing: **busy in one place** (fanning, grazing) or **moving around** (foraging, wandering), in whatever words you give it in Setup. The morning summary then reads "Bamboo shrimp was fanning for 2 h 10 min and foraging for 35 min."

**Slow movers' paths.** Each morning the night's proxy stream is sampled a minute apart. Anything found in roughly the same place minute after minute is a slow mover: snails on the glass, the pleco grazing, the shrimp on the move. Their paths are drawn over a picture of the empty tank, on the tank page under Last night.

**Heatmaps.** Where movement happens, added up per tank over the last weeks, day and night separately, on the Trends page. A front view, so it says nothing about depth.

**Danger watches.** The app reports and never advises, with one exception: something genuinely dangerous. Then it says what it saw and, in one line, what that can mean. Never what to do.

* **Filter bubbles stopped.** Draw one box over a sponge filter's bubble stream (Draw boxes) and switch on "Tell me if the filter bubbles stop" in Setup. It learns what the running stream looks like under each lighting level, judges on the middle of the last 30 seconds so fish swimming through don't fool it, and alerts after 3 minutes of near stillness, even with notifications set to quiet. It holds off during maintenance, light switches, camera moves, and when the picture itself is frozen or black. If the camera is on another tank, the status dot says the bubbles aren't being watched.
* **Fish crowding the surface** for 30 minutes, not after feeding ("That can mean low oxygen"), **a tank much stiller than usual** for a daytime hour, compared with the same hours on previous days ("That can mean a heater or oxygen problem"), and **something fish sized that appeared and hasn't moved** for over half an hour in daylight, floating at the surface or lying in the open ("That can be a dead fish"). These show on the status dot and in the morning summary but don't send alerts until they've been tuned on real footage.

**Quiet by design.** Phone notifications (through the free ntfy app) default to one morning summary a day, plus one message if BubbleCam itself breaks: camera not sending pictures, picture frozen or black, recording stopped, USB stick missing, storage full. Each problem has to persist before it counts, and won't repeat within 6 hours. "An animal came out" and burst messages exist but are off unless you turn them on.

**Staying up.** The app checks in with the Pi's service manager every 10 seconds and gets restarted if it stops making progress. The Pi's hardware watchdog reboots it if the whole system freezes. For the case where the Pi dies outright, paste a free Healthchecks.io ping address into Setup: the Pi pings it every few minutes while all is well, and that service tells you when the pings stop.

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
* offers to show the live view full screen on the monitor at startup (the ⋯ button in the corner has an exit, restart and shut down)
* lets Setup turn on phone access and restart or shut down the Pi with a button

At the end it prints the address to open on your phone. To update later, run `~/BubbleCam/deploy/install.sh` again.

Useful afterwards:

```sh
journalctl -u bubblecam -f         # watch the app's log
sudo systemctl restart bubblecam   # restart after editing config.yaml
v4l2-ctl -d /dev/video0 -l         # camera controls, for tuning exposure and gain in config.yaml
```

At the default quality, real time footage takes about 16 GB a day: a 64 GB stick holds about 3 days, 128 GB about a week. Starred events are never deleted.

## First night checklist

1. The first tank sets itself up a few seconds after the camera starts. Give it a name in Setup.
2. In Setup, under **Who to watch**, add your animals (Pleco, Bamboo shrimp...).
3. Tap **Draw boxes**: for each animal, where it hangs out and its home if it has one; then areas to ignore (bubbles, filter outflow, swaying plants). Save.
4. On the tank page, tap an animal with a home and answer "home" or "out" whenever you can see which it is. A few of each is enough to start.
5. Watch the live picture with boxes on: green boxes are what the detector sees moving. If sensor grain makes green speckles in the dark, raise `diff_threshold` in config.yaml. If shrimp set off the pleco's area, raise `min_blob_area`, or turn the tank's sensitivity down in Advanced.
6. In the morning: last night's summary and timelapse on the tank page. Mark anything that's wrong; that's how it gets tuned.

## Tuning on real footage

The first nights are a test set. Note what really happened (the pleco out from 23:14 to 23:19, a feeding at 19:02, a chase at 01:40, two hours with nothing at all), save the tank's layout, and replay the footage through detection offline:

```sh
curl -s localhost:8080/api/tanks/1/layout > layout.json
.venv/bin/python -m bubblecam.replay /mnt/bubblecam/data/rec/2026-10-14 --layout layout.json \
    --labels labels.json --start 19:00 --end 09:00 --every 2
```

It prints every moment it found, what each animal was doing when, which labels it matched, what it missed and what it made up. Change a setting, replay, compare. Keep the labelled nights: `python -m bubblecam.replay --help` shows the label format, and anything that used to score well and stops doing so after a change is a regression.

The **Advanced** page shows how often each kind of moment gets marked wrong, plus the work per frame and the Pi's load. If the Pi falls behind, the app analyses every second or third frame by itself and says so there; recording is never thinned.

To set up a second tank, point the camera at it and tap "It's a new tank" on the home screen (or "Set up a new tank" in Setup).

## Development

```sh
pip install -r requirements.txt pytest
pytest
```

Layout:

* `camera.py`: frame sources, including a fake tank for testing without hardware (`--synthetic`)
* `tanks.py`: tanks, spotlights, box geometry
* `viewmatch.py`: which tank the camera sees, and how far it moved
* `detector.py`: motion
* `events.py`: spotlight moments, bursts, feeding
* `presence.py`: home or out
* `recorder.py`: real time segments, per frame scores, and the per second side stream
* `timelapse.py`: speed curve and rendering
* `storage.py`: cutting moments and cleaning up
* `health.py`: system checks and watchdogs
* `danger.py`: filter bubbles, surface crowding, daytime stillness, still objects
* `tracks.py`: short tracks, chases, busy in one place vs moving around
* `maps.py`: heatmaps and slow movers' paths
* `replay.py`: offline replay and scoring of footage
* `summary.py`: nights and trends
* `notify.py`: phone notifications
* `core.py`: wiring
* `web/`: the API and the screens, with all styling in the tokens at the top of `web/static/style.css`

With `--synthetic`, setting `BUBBLECAM_SYNTH_CTRL` to a file containing `seed dx dy` plus any of `feed`, `nobubbles`, `night`, `chase`, `dead`, `fanning`, `foraging`, `nocam`, `freeze` drives the fake tank while the app runs: switch tank, shift the camera, feeding rush, filter stopped, lights off, a chase, a dead fish, what the shrimp does, camera unplugged, picture frozen.
