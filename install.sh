#!/usr/bin/env bash
#
# Dependency installer for the ACOM Remote Control project.
# Target: Raspberry Pi OS Lite (64-bit, Debian Bookworm), but works on any
# Debian/Ubuntu-based system with apt.
#
# Installs the system packages and Python packages needed to build and run
# the acom-control backend (FastAPI + pyserial + pymodbus). See
# acom-control/README.md for hardware setup and how to actually run it
# after this script finishes.
#
# Usage: bash install.sh

set -euo pipefail

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Run this as your normal user (e.g. 'pi'), not root - it uses sudo where needed." >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$HOME/acom-control-venv"

echo "==> Updating apt package lists"
sudo apt-get update

echo "==> Installing system packages"
sudo apt-get install -y \
  python3 \
  python3-venv \
  python3-pip \
  git \
  avahi-daemon \
  usbutils

echo "==> Adding $(whoami) to the 'dialout' group (required for serial port access)"
sudo usermod -aG dialout "$(whoami)"

echo "==> Creating Python virtual environment at $VENV_DIR"
python3 -m venv "$VENV_DIR"

echo "==> Installing Python packages into the virtual environment"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements.txt"

UDEV_RULE_SRC="$REPO_DIR/acom-control/setup/99-acom-amp.rules"
UDEV_RULE_DST="/etc/udev/rules.d/99-acom-amp.rules"
UDEV_RULE_INSTALLED=0

echo "==> Installing udev rule for a stable /dev/acom-amp symlink"
ADAPTER_SERIAL=""
for dev in /dev/ttyUSB* /dev/ttyACM*; do
  [[ -e "$dev" ]] || continue
  # Capture attribute-walk's full output before grepping it - piping it
  # straight into `grep -m1` lets grep exit (closing the pipe) while
  # udevadm is still writing, which kills udevadm with SIGPIPE; under
  # `set -o pipefail` that non-zero status aborts the whole script.
  walk="$(udevadm info --name="$dev" --attribute-walk 2>/dev/null)"
  serial="$(printf '%s\n' "$walk" | grep -m1 '{serial}' | sed -E 's/.*=="([^"]+)".*/\1/')"
  if [[ -n "$serial" ]]; then
    ADAPTER_SERIAL="$serial"
    echo "    Found USB-serial adapter at $dev (serial=$ADAPTER_SERIAL)"
    break
  fi
done

if [[ -n "$ADAPTER_SERIAL" ]]; then
  sed "s/REPLACE_WITH_ADAPTER_SERIAL/$ADAPTER_SERIAL/" "$UDEV_RULE_SRC" | sudo tee "$UDEV_RULE_DST" >/dev/null
  sudo udevadm control --reload-rules
  sudo udevadm trigger
  UDEV_RULE_INSTALLED=1
  echo "    Installed $UDEV_RULE_DST - /dev/acom-amp should now exist."
else
  echo "    No USB-serial adapter detected (plug the RS-232 adapter in and re-run," \
       "or install the rule manually - see $UDEV_RULE_SRC for instructions)."
fi

SERVICE_SRC="$REPO_DIR/acom-control/setup/acom-control.service"
SERVICE_DST="/etc/systemd/system/acom-control.service"

echo "==> Setting up and starting the acom-control systemd service"
if ! id acom-control &>/dev/null; then
  sudo useradd --system --no-create-home --groups dialout acom-control
fi
sed \
  -e "s#^WorkingDirectory=.*#WorkingDirectory=$REPO_DIR/acom-control#" \
  -e "s#^Environment=PATH=.*#Environment=PATH=$VENV_DIR/bin:/usr/bin:/bin#" \
  -e "s#^ExecStart=.*#ExecStart=$VENV_DIR/bin/uvicorn backend.app:app --host 0.0.0.0 --port 8000#" \
  "$SERVICE_SRC" | sudo tee "$SERVICE_DST" >/dev/null
sudo systemctl daemon-reload
# Opening the serial port raises DTR, which has been observed to toggle
# the amp's power state on real 1200S hardware (CLAUDE.md open question
# 4/5) - accepted as a known risk rather than gated here, so this starts
# the service unattended rather than stopping short.
sudo systemctl enable --now acom-control

cat <<EOF

==> Done.

Notes:
- You were added to the 'dialout' group for serial port access.
  Log out and back in (or reboot) for that to take effect.
- Python virtual environment: $VENV_DIR
  Activate it with: source $VENV_DIR/bin/activate
EOF

if [[ "$UDEV_RULE_INSTALLED" -eq 0 ]]; then
  cat <<EOF
- The udev rule for /dev/acom-amp was NOT installed (no adapter detected).
  Plug in the USB-RS232 adapter and re-run this script, or follow the
  manual steps in acom-control/setup/99-acom-amp.rules.
EOF
fi

cat <<EOF
- The acom-control systemd service is installed at $SERVICE_DST and
  running (enabled to start on boot too). FYI: opening the serial port
  raises DTR, which has been observed to toggle the amp's power state on
  real 1200S hardware (see CLAUDE.md open question 4/5) - so this start,
  and any future restart/crash-recovery, may have just flipped the amp's
  power state if it was already on.
  journalctl -u acom-control -f
- Re-run this script any time requirements.txt changes.
EOF
