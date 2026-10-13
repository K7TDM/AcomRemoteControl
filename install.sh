#!/usr/bin/env bash
#
# Dependency installer for the ACOM Remote Control project.
# Target: Raspberry Pi OS Lite (64-bit, Debian Bookworm), but works on any
# Debian/Ubuntu-based system with apt.
#
# Installs the system packages and Python packages needed to build and run
# the acom-control backend (FastAPI + pyserial + pymodbus). Application code
# isn't in this repo yet - this script only sets up the dependencies.
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

cat <<EOF

==> Done.

Notes:
- You were added to the 'dialout' group for serial port access.
  Log out and back in (or reboot) for that to take effect.
- Python virtual environment: $VENV_DIR
  Activate it with: source $VENV_DIR/bin/activate
- Application code (acom-control/) will be added to this repo once the
  backend/frontend are built - re-run this script if requirements.txt
  changes.
EOF
