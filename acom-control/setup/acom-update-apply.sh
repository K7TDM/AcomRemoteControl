#!/usr/bin/env bash
#
# Installed by install.sh at /usr/local/bin/acom-update-apply. Invoked only
# via the tightly-scoped NOPASSWD sudoers rule in
# acom-control-update.sudoers, by the unprivileged acom-control service
# account - see app.py's /api/update/* endpoints, behind the header
# button's popover and the daily background check (built because running
# multi-line SSH commands from a phone is a pain).
#
# Versioning is git tags (vX.Y.Z), not raw commits - see CHANGELOG.md.
# Deliberately keyed to the latest tag rather than the tip of main, for
# two reasons: so ordinary commits/pushes to main don't trigger an
# "update available" notice (only a deliberate `git tag vX.Y.Z && git
# push --tags` does), and so this never auto-deploys unreleased,
# possibly-unfinished work just because it happened to land on main.
#
# Two modes, both invoked with a fixed, no-extra-argument command line so
# the sudoers rule can allow exactly these and nothing else:
#   acom-update-apply check   - read-only: fetch tags + compare, never
#                                pulls or restarts. Used by the daily
#                                background check and the header button.
#   acom-update-apply         - fast-forwards to the latest tag,
#                                reinstalls requirements if changed, and
#                                restarts the service.
#
# Runs as root (that's what the sudoers rule grants), but git and pip run
# as REPO_OWNER (whoever ran install.sh) instead of root, so pulled files
# don't end up root-owned and break a future manual `git pull`/`bash
# install.sh` run by that user.
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

git_as_owner() {
  sudo -u "$REPO_OWNER" -H git -C "$REPO_DIR" "$@"
}

MODE="${1:-apply}"

echo "acom-update-apply: fetching tags"
git_as_owner fetch --tags --quiet

# Latest release tag by version sort, not by tag-creation date - so this
# can't be confused by a tag pushed out of order. Only vX.Y.Z-shaped tags
# count; this repo shouldn't have other tag shapes, but don't assume.
LATEST_TAG="$(git_as_owner tag --list 'v*' --sort=-v:refname | head -n1)"
CURRENT_VERSION="$(git_as_owner describe --tags --always)"

if [[ "$MODE" == "check" ]]; then
  echo "current=$CURRENT_VERSION"
  if [[ -z "$LATEST_TAG" ]]; then
    echo "latest=none"
    echo "update_available=no"
    exit 0
  fi
  echo "latest=$LATEST_TAG"
  if git_as_owner merge-base --is-ancestor "$LATEST_TAG" HEAD; then
    echo "update_available=no"
  else
    echo "update_available=yes"
  fi
  exit 0
fi

if [[ -z "$LATEST_TAG" ]]; then
  echo "acom-update-apply: no tagged release exists yet (current: $CURRENT_VERSION) - nothing to update to"
  exit 0
fi

if git_as_owner merge-base --is-ancestor "$LATEST_TAG" HEAD; then
  echo "acom-update-apply: already at or past $LATEST_TAG ($CURRENT_VERSION) - nothing to restart"
  exit 0
fi

echo "acom-update-apply: updating $CURRENT_VERSION -> $LATEST_TAG"
git_as_owner merge --ff-only "$LATEST_TAG"

echo "acom-update-apply: updating Python packages"
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
