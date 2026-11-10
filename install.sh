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
# Usage: bash install.sh             (also shows the dashboard full screen
#                                     on the Pi's own touchscreen/monitor)
#        bash install.sh --no-kiosk  (headless - dashboard over the network
#                                     only, nothing shown on an attached screen)

set -euo pipefail

KIOSK=1
for arg in "$@"; do
  case "$arg" in
    --no-kiosk) KIOSK=0 ;;
    *) echo "Unknown option: $arg (the only option is --no-kiosk)" >&2; exit 2 ;;
  esac
done

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Run this as your normal user (e.g. 'pi'), not root - it uses sudo where needed." >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$HOME/acom-control-venv"
INSTALL_USER="$(whoami)"

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

echo "==> Hostname (so the dashboard is reachable at <name>.local)"
CURRENT_HOSTNAME="$(hostname)"
echo "  Current hostname: $CURRENT_HOSTNAME"
if [[ -t 0 ]]; then
  echo "  Press Enter to keep it, or type a new one (e.g. acomcontrol)."
  echo "  Running more than one of these on the same network? Each Pi needs"
  echo "  its own unique name here, or they'll collide on .local lookups."
  NEW_HOSTNAME=""
  while [[ -z "$NEW_HOSTNAME" ]]; do
    read -r -p "Hostname [$CURRENT_HOSTNAME]: " INPUT_HOSTNAME
    INPUT_HOSTNAME="${INPUT_HOSTNAME:-$CURRENT_HOSTNAME}"
    INPUT_HOSTNAME="${INPUT_HOSTNAME,,}"
    if [[ "$INPUT_HOSTNAME" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then
      NEW_HOSTNAME="$INPUT_HOSTNAME"
    else
      echo "  Invalid hostname - lowercase letters, digits, and hyphens only (not at the start/end)." >&2
    fi
  done
  if [[ "$NEW_HOSTNAME" != "$CURRENT_HOSTNAME" ]]; then
    sudo hostnamectl set-hostname "$NEW_HOSTNAME"
    # 127.0.1.1 is the Debian/Pi OS convention for the machine's own name,
    # separate from 127.0.0.1 - update it so the new name resolves locally
    # too, not just via mDNS from other machines on the LAN.
    if grep -q "^127\.0\.1\.1" /etc/hosts; then
      sudo sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$NEW_HOSTNAME/" /etc/hosts
    else
      printf '127.0.1.1\t%s\n' "$NEW_HOSTNAME" | sudo tee -a /etc/hosts >/dev/null
    fi
    # Restart (not just rely on a later reboot) so avahi advertises the new
    # name immediately - it was only just installed above in this same run.
    sudo systemctl restart avahi-daemon
  fi
  CURRENT_HOSTNAME="$NEW_HOSTNAME"
else
  echo "  No interactive terminal - keeping the current hostname ($CURRENT_HOSTNAME)."
fi

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
  # systemd-journal is needed so GET /api/logs (Setup screen's "Download
  # Logs" button) can read this unit's own journal entries as this
  # unprivileged user - without it, journalctl exits with a permission
  # error. If the user already exists from before this was added, fix it
  # up below instead of re-running useradd.
  sudo useradd --system --no-create-home --groups dialout,systemd-journal acom-control
else
  sudo usermod -aG systemd-journal acom-control
fi
sed \
  -e "s#^WorkingDirectory=.*#WorkingDirectory=$REPO_DIR/acom-control#" \
  -e "s#^Environment=PATH=.*#Environment=PATH=$VENV_DIR/bin:/usr/bin:/bin#" \
  -e "s#^ExecStart=.*#ExecStart=$VENV_DIR/bin/uvicorn backend.app:app --host 0.0.0.0 --port 8000#" \
  "$SERVICE_SRC" | sudo tee "$SERVICE_DST" >/dev/null

echo "==> Giving the acom-control service account access to the repo and venv"
# The repo and venv live under this user's home, but the service runs as
# acom-control. Current Raspberry Pi OS creates home directories as 0700,
# so without this systemd can't even chdir into WorkingDirectory and the
# service crash-loops with status=200/CHDIR. o+x (traverse only, not
# list) on every directory above the repo and venv is all it needs - the
# repo and venv themselves are already world-readable.
for dir in "$REPO_DIR" "$VENV_DIR"; do
  dir="$(dirname "$dir")"
  while [[ "$dir" != "/" ]]; do
    sudo chmod o+x "$dir"
    dir="$(dirname "$dir")"
  done
done
# The Setup screen saves config.json in place as acom-control. The service
# runs with Group=dialout, which this user was added to above too, so a
# group-writable file stays editable by both.
CONFIG_FILE="$REPO_DIR/acom-control/config.json"
sudo chgrp dialout "$CONFIG_FILE"
sudo chmod 664 "$CONFIG_FILE"

sudo systemctl daemon-reload
sudo systemctl enable acom-control
# Opening the serial port raises DTR, which has been observed to toggle
# the amp's power state on real 1200S hardware (CLAUDE.md open question
# 4/5) - accepted as a known risk rather than gated here, so this starts
# the service unattended rather than stopping short. Using `restart`
# rather than `enable --now`'s implicit `start` so this also actually
# takes effect when this script is re-run on top of a service that's
# already active (e.g. after a `git pull`) - `systemctl start` is a
# no-op on an already-running unit, which silently defeated the "re-run
# this script any time requirements.txt changes" note below until now.
sudo systemctl restart acom-control

echo "==> Installing the self-update helper (dashboard's update icon + daily check)"
UPDATE_SCRIPT_SRC="$REPO_DIR/acom-control/setup/acom-update-apply.sh"
UPDATE_SCRIPT_DST="/usr/local/bin/acom-update-apply"
sed \
  -e "s#__REPO_DIR__#$REPO_DIR#" \
  -e "s#__REPO_OWNER__#$INSTALL_USER#" \
  -e "s#__VENV_DIR__#$VENV_DIR#" \
  "$UPDATE_SCRIPT_SRC" | sudo tee "$UPDATE_SCRIPT_DST" >/dev/null
sudo chown root:root "$UPDATE_SCRIPT_DST"
sudo chmod 755 "$UPDATE_SCRIPT_DST"

SUDOERS_SRC="$REPO_DIR/acom-control/setup/acom-control-update.sudoers"
SUDOERS_DST="/etc/sudoers.d/acom-control-update"
SUDOERS_TMP="$(mktemp)"
cp "$SUDOERS_SRC" "$SUDOERS_TMP"
# Validate before installing - a syntax error in /etc/sudoers.d can break
# sudo system-wide, so this must never be copied in unchecked.
sudo visudo -cf "$SUDOERS_TMP"
sudo cp "$SUDOERS_TMP" "$SUDOERS_DST"
sudo chown root:root "$SUDOERS_DST"
sudo chmod 440 "$SUDOERS_DST"
rm -f "$SUDOERS_TMP"

if [[ "$KIOSK" -eq 1 ]]; then
  echo "==> Setting up the kiosk display (Chromium full screen on the attached screen)"
  # Same code path as Setup -> Display's "Raspberry Pi display" switch, so
  # the two can't drift apart (see acom-update-apply.sh's kiosk-on mode).
  sudo "$UPDATE_SCRIPT_DST" kiosk-on
fi

cat <<EOF

==> Done.

Notes:
- Reachable on your local network at http://$CURRENT_HOSTNAME.local:8000
  (via mDNS/avahi - takes a moment after boot, and some networks/devices
  don't support .local names; the dashboard's own footer also shows a
  plain IP address as a fallback).
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
- To update later without SSHing in again: the dashboard checks for
  updates once a day on its own and shows an icon in the top bar when
  one's available - click it, then "Update Now" to pull and restart
  from there. (It works by granting the acom-control service account
  passwordless sudo for exactly seven fixed invocations of one script,
  /usr/local/bin/acom-update-apply - see
  acom-control/setup/acom-control-update.sudoers if you want to review
  or revoke that.) The manual fallback is: sudo acom-update-apply
EOF

if [[ "$KIOSK" -eq 1 ]]; then
  cat <<EOF
- Kiosk display: the dashboard now runs full screen on the Pi's attached
  screen, and starts at every boot (service: acom-kiosk). The mouse
  cursor stays hidden until a mouse is actually used; touch never shows
  it. To get a text console back: plug in a keyboard and press
  Ctrl+Alt+F2, or turn the kiosk off with
    sudo systemctl disable --now acom-kiosk
  Touch Display 2: add  dtoverlay=vc4-kms-dsi-ili9881-7inch  (or the 5"
  overlay) to /boot/firmware/config.txt and reboot. Its panel is natively
  portrait, so it may also need rotating - see acom-control/README.md.
  The original 7" touchscreen and HDMI monitors need no setup.
EOF
fi
