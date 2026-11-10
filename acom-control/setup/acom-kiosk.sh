#!/usr/bin/env bash
#
# Installed by `install.sh --kiosk` at /usr/local/bin/acom-kiosk. Run by
# acom-kiosk.service inside cage (a single-app Wayland display server), so
# Chromium fills the Pi's attached screen - the official touchscreen or an
# HDMI monitor - with the dashboard, and nothing else.
#
# Waits for the dashboard backend to answer before starting the browser:
# at boot the kiosk can come up before acom-control is listening, and a
# browser that loads too early just sits on an error page.

set -u

DASHBOARD="http://localhost:8000"
# kiosk=1 turns on the dashboard's kiosk behaviors (hidden cursor until a
# mouse moves, no text selection or long-press menu) - see app.js.
URL="$DASHBOARD/?kiosk=1"
# Its own profile, so the theme and other per-browser settings persist
# across reboots without mixing with anything else on the Pi.
PROFILE_DIR="$HOME/.config/acom-kiosk"

until curl -fsS -o /dev/null --max-time 2 "$DASHBOARD/"; do
  sleep 2
done

BROWSER="$(command -v chromium || command -v chromium-browser || true)"
if [[ -z "$BROWSER" ]]; then
  echo "acom-kiosk: Chromium isn't installed - re-run install.sh --kiosk" >&2
  exit 1
fi

# A power cut leaves the profile marked as crashed, and Chromium would then
# show a "didn't shut down correctly" bar over the dashboard on next boot.
PREFS="$PROFILE_DIR/Default/Preferences"
if [[ -f "$PREFS" ]]; then
  sed -i -e 's/"exited_cleanly":false/"exited_cleanly":true/' \
         -e 's/"exit_type":"Crashed"/"exit_type":"Normal"/' "$PREFS"
fi

# Chromium's SingletonLock records the hostname + PID that owns this
# profile, to stop two machines sharing a network-mounted profile from
# corrupting it. This profile is never shared - only ever this one Pi,
# one Chromium at a time (systemd's Restart=always serializes that) - but
# the hostname half of that check breaks the moment the Pi's own hostname
# changes (e.g. install.sh's hostname prompt, or a hostname set differently
# on a reflash): a lock file still naming the *old* hostname makes Chromium
# think "another computer" has it locked and refuse to start at all
# (confirmed live 2026-11-09 - every launch failed with "Error: Can't open
# display:", which turned out to be this, not an actual display problem).
# Always safe to clear before every launch, for the single-instance reason
# above.
rm -f "$PROFILE_DIR/SingletonLock" "$PROFILE_DIR/SingletonCookie" "$PROFILE_DIR/SingletonSocket"

# --autoplay-policy: the dashboard's alarm sounds play without the screen
# having been touched first (see app.js's unlockAlarmAudio()).
exec "$BROWSER" \
  --kiosk \
  --user-data-dir="$PROFILE_DIR" \
  --ozone-platform=wayland \
  --noerrdialogs \
  --disable-infobars \
  --no-first-run \
  --disable-session-crashed-bubble \
  --disable-features=Translate \
  --check-for-update-interval=31536000 \
  --password-store=basic \
  --overscroll-history-navigation=0 \
  --disable-pinch \
  --hide-scrollbars \
  --autoplay-policy=no-user-gesture-required \
  "$URL"
