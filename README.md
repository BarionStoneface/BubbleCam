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

**Tanks, and a camera that moves.** Each tank keeps its own boxes, animals, settings and history. The app recognises which tank the camera is looking at by matching distinctive points in the rocks, wood and gravel against reference pictures, so you can move the camera between tanks and it switches by itself. If the camera gets bumped, it measures exactly how far and moves the boxes to match. While it's unsure (the seconds after a move), it records but makes no events, so nothing gets filed against the wrong tank. If it doesn't recognise the view at all, the home screen asks which tank this is, or offers to set up a new one.

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

**Danger watches.** The app reports and never advises, with one exception: something genuinely dangerous. Then it says what it saw and, in one line, what that can mean. Never what to do.

* **Filter bubbles stopped.** Draw one box over a sponge filter's bubble stream (Draw boxes) and switch on "Tell me if the filter bubbles stop" in Setup. It learns what the running stream looks like under each lighting level, judges on the middle of the last 30 seconds so fish swimming through don't fool it, and alerts after 3 minutes of near stillness, even with notifications set to quiet. It holds off during maintenance, light switches, camera moves, and when the picture itself is frozen or black. If the camera is on another tank, the status dot says the bubbles aren't being watched.
* **Fish crowding the surface** for 30 minutes, not after feeding ("That can mean low oxygen"), and **a tank much stiller than usual** for a daytime hour, compared with the same hours on previous days ("That can mean a heater or oxygen problem"). These two show on the status dot and in the morning summary but don't send alerts until they've been tuned on real footage.

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

1. The first tank sets itself up a few seconds after the camera starts. Give it a name in Setup.
2. In Setup, under **Who to watch**, add your animals (Pleco, Bamboo shrimp...).
3. Tap **Draw boxes**: for each animal, where it hangs out and its home if it has one; then areas to ignore (bubbles, filter outflow, swaying plants). Save.
4. On the tank page, tap an animal with a home and answer "home" or "out" whenever you can see which it is. A few of each is enough to start.
5. Watch the live picture with boxes on: green boxes are what the detector sees moving. If sensor grain makes green speckles in the dark, raise `diff_threshold` in config.yaml. If shrimp set off the pleco's area, raise `min_blob_area`, or turn the tank's sensitivity down in Advanced.
6. In the morning: last night's summary and timelapse on the tank page. Mark anything that's wrong; that's how it gets tuned.

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
* `danger.py`: filter bubbles, surface crowding, daytime stillness
* `summary.py`: nights and trends
* `notify.py`: phone notifications
* `core.py`: wiring
* `web/`: the API and the screens, with all styling in the tokens at the top of `web/static/style.css`

With `--synthetic`, setting `BUBBLECAM_SYNTH_CTRL` to a file containing `seed dx dy [feed] [nobubbles]` lets you switch the fake camera to another tank, shift it, set off a feeding rush, or stop the filter bubbles while the app runs.
