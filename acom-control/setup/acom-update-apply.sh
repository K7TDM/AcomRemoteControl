#!/usr/bin/env bash
#
# Installed by install.sh at /usr/local/bin/acom-update-apply. Invoked only
# via the tightly-scoped NOPASSWD sudoers rule in
# acom-control-update.sudoers, by the unprivileged acom-control service
# account - see app.py's /api/update/* endpoints, behind the Setup
# screen's "Check for Updates" button and the daily background check
# (built because running multi-line SSH commands from a phone is a pain).
#
# Two modes, both invoked with a fixed, no-extra-argument command line so
# the sudoers rule can allow exactly these and nothing else:
#   acom-update-apply check   - read-only: fetch + compare, never pulls or
#                                restarts. Used by the daily background
#                                check and the "Check for Updates" button.
#   acom-update-apply         - pulls, reinstalls requirements if changed,
#                                and restarts the service.
#
# Runs as root (that's what the sudoers rule grants), but git pull and pip
# install run as REPO_OWNER (whoever ran install.sh) instead of root, so
# pulled files don't end up root-owned and break a future manual `git
# pull`/`bash install.sh` run by that user.
#
# Deliberately fails loudly and stops (set -e) rather than attempting
# anything clever on error - an unattended, remotely-triggered update that
# goes wrong should leave the old, working code running, not restart into
# a broken state.
set -euo pipefail

REPO_DIR="__REPO_DIR__"
REPO_OWNER="__REPO_OWNER__"
VENV_DIR="__VENV_DIR__"

export GIT_TERMINAL_PROMPT=0  # fail fast instead of hanging on an auth prompt with no TTY

MODE="${1:-apply}"

if [[ "$MODE" == "check" ]]; then
  echo "acom-update-apply: fetching to check for updates (not applying)"
  sudo -u "$REPO_OWNER" -H git -C "$REPO_DIR" fetch --quiet
  CURRENT_COMMIT="$(sudo -u "$REPO_OWNER" git -C "$REPO_DIR" rev-parse HEAD)"
  UPSTREAM_COMMIT="$(sudo -u "$REPO_OWNER" git -C "$REPO_DIR" rev-parse '@{u}')"
  echo "current=$CURRENT_COMMIT"
  echo "upstream=$UPSTREAM_COMMIT"
  if [[ "$CURRENT_COMMIT" == "$UPSTREAM_COMMIT" ]]; then
    echo "update_available=no"
  else
    echo "update_available=yes"
  fi
  exit 0
fi

echo "acom-update-apply: pulling latest changes in $REPO_DIR"
BEFORE_COMMIT="$(sudo -u "$REPO_OWNER" git -C "$REPO_DIR" rev-parse HEAD)"
sudo -u "$REPO_OWNER" -H git -C "$REPO_DIR" pull --ff-only
AFTER_COMMIT="$(sudo -u "$REPO_OWNER" git -C "$REPO_DIR" rev-parse HEAD)"

if [[ "$BEFORE_COMMIT" == "$AFTER_COMMIT" ]]; then
  echo "acom-update-apply: already up to date at $AFTER_COMMIT - nothing to restart"
  exit 0
fi

echo "acom-update-apply: $BEFORE_COMMIT -> $AFTER_COMMIT, updating Python packages"
sudo -u "$REPO_OWNER" -H "$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements.txt"

echo "acom-update-apply: re-applying systemd service file (in case it changed)"
SERVICE_SRC="$REPO_DIR/acom-control/setup/acom-control.service"
SERVICE_DST="/etc/systemd/system/acom-control.service"
sed \
  -e "s#^WorkingDirectory=.*#WorkingDirectory=$REPO_DIR/acom-control#" \
  -e "s#^Environment=PATH=.*#Environment=PATH=$VENV_DIR/bin:/usr/bin:/bin#" \
  -e "s#^ExecStart=.*#ExecStart=$VENV_DIR/bin/uvicorn backend.app:app --host 0.0.0.0 --port 8000#" \
  "$SERVICE_SRC" > "$SERVICE_DST"
systemctl daemon-reload

echo "acom-update-apply: restarting acom-control"
systemctl restart --no-block acom-control
