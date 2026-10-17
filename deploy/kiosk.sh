#!/bin/sh
# Fullscreen live view on the monitor plugged into the Pi (added to autostart by install.sh).
until curl -s -o /dev/null http://localhost:8080/api/status; do sleep 2; done
BROWSER=$(command -v chromium-browser || command -v chromium)
exec "$BROWSER" --kiosk --noerrdialogs --disable-infobars --incognito --password-store=basic --no-first-run "http://localhost:8080/?kiosk#dash"
