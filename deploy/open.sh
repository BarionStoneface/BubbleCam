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
exec "$BROWSER" --app="http://localhost:8080/#dash" --start-maximized --class=BubbleCam \
  --user-data-dir="$HOME/.config/bubblecam-window" --password-store=basic --no-first-run --noerrdialogs
