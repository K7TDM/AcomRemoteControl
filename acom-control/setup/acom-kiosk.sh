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
#
# `acom-kiosk --blank-cursor-theme` (the unit's ExecStartPre) instead
# writes an invisible cursor theme for cage - see below.

set -u

# Cage draws its own arrow (the theme's left_ptr) wherever the pointer is,
# and with only a touchscreen nothing ever replaces it: the page's
# cursor-hiding CSS (app.js's initKioskMode()) only applies once a real
# mouse moves over Chromium. So cage gets a theme whose cursors are a
# single transparent pixel (XCURSOR_THEME=acom-blank in the unit), and
# Chromium is started with the normal theme below - a real mouse still
# shows an arrow, which the page hides again after a few seconds idle.
BLANK_CURSOR_THEME="acom-blank"
if [[ "${1:-}" == "--blank-cursor-theme" ]]; then
  THEME_DIR="$HOME/.icons/$BLANK_CURSOR_THEME"
  mkdir -p "$THEME_DIR/cursors"
  printf '[Icon Theme]\nName=%s\n' "$BLANK_CURSOR_THEME" > "$THEME_DIR/index.theme"
  # Xcursor file: header, one table-of-contents entry, one 1x1 image chunk
  # whose single ARGB pixel is fully transparent.
  python3 - "$THEME_DIR/cursors/left_ptr" <<'EOF'
import struct, sys
image_type, size = 0xFFFD0002, 24
data = b"Xcur" + struct.pack("<III", 16, 0x10000, 1)
data += struct.pack("<III", image_type, size, 28)
data += struct.pack("<IIIIIIIII", 36, image_type, size, 1, 1, 1, 0, 0, 0) + struct.pack("<I", 0)
open(sys.argv[1], "wb").write(data)
EOF
  ln -sf left_ptr "$THEME_DIR/cursors/default"
  exit 0
fi
unset XCURSOR_THEME

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
  "$URL"
