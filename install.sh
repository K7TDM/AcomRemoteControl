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
  serial="$(udevadm info --name="$dev" --attribute-walk 2>/dev/null | grep -m1 '{serial}' | sed -E 's/.*=="([^"]+)".*/\1/')"
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

echo "==> Setting up the acom-control systemd service (not starting it yet)"
if ! id acom-control &>/dev/null; then
  sudo useradd --system --no-create-home --groups dialout acom-control
fi
sed \
  -e "s#^WorkingDirectory=.*#WorkingDirectory=$REPO_DIR/acom-control#" \
  -e "s#^Environment=PATH=.*#Environment=PATH=$VENV_DIR/bin:/usr/bin:/bin#" \
  -e "s#^ExecStart=.*#ExecStart=$VENV_DIR/bin/uvicorn backend.app:app --host 0.0.0.0 --port 8000#" \
  "$SERVICE_SRC" | sudo tee "$SERVICE_DST" >/dev/null
sudo systemctl daemon-reload

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
- The acom-control systemd service was installed at $SERVICE_DST but is
  NOT started or enabled yet.
- IMPORTANT before you start it: opening the serial port (which happens
  every time this service starts, restarts, or crash-recovers) raises
  DTR, and on real 1200S hardware a DTR pulse has been observed to TOGGLE
  the amp's power state - so starting/restarting this service while the
  amp is powered on and in use is not risk-free. See CLAUDE.md open
  question 4 before relying on it unattended.
- When ready: sudo systemctl enable --now acom-control
  journalctl -u acom-control -f
- Re-run this script any time requirements.txt changes.
EOF
