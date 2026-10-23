#!/bin/sh
# Open BubbleCam on the Pi's screen.
#   open.sh          a normal window: minimise, resize, close (closing it doesn't stop recording)
#   open.sh full     full screen, the way the monitor used to start
until curl -s -o /dev/null http://localhost:8080/api/status; do sleep 2; done
BROWSER=$(command -v chromium || command -v chromium-browser)
if [ "$1" = "full" ]; then
  exec "$BROWSER" --kiosk --noerrdialogs --disable-infobars --incognito --password-store=basic --no-first-run \
    "http://localhost:8080/?kiosk#dash"
fi
# Its own browser profile, so it's a separate app window with its own icon, whatever else is open.
# A sensible size the first time; after that it opens the size and place you last left it.
PROFILE="$HOME/.config/bubblecam-window"
SIZE=""
[ -d "$PROFILE" ] || SIZE="--window-size=1024,700"
exec "$BROWSER" --app="http://localhost:8080/#dash" $SIZE --class=BubbleCam \
  --user-data-dir="$PROFILE" --password-store=basic --no-first-run --noerrdialogs
