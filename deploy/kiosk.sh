#!/bin/sh
# Fullscreen live view on the monitor plugged into the Pi.
# Add this script's path to ~/.config/labwc/autostart on Raspberry Pi OS desktop.
until curl -s -o /dev/null http://localhost:8080/api/status; do sleep 2; done
exec chromium-browser --kiosk --noerrdialogs --disable-infobars --incognito "http://localhost:8080/?kiosk#live"
