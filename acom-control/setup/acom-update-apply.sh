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
# Five modes, all invoked with a fixed, no-extra-argument command line so
# the sudoers rule can allow exactly these and nothing else:
#   acom-update-apply check   - read-only: fetch tags + compare, never
#                                pulls or restarts. Used by the daily
#                                background check and the header button.
#   acom-update-apply         - fast-forwards to the latest tag,
#                                reinstalls requirements if changed, and
#                                restarts the service.
#   acom-update-apply check-beta / beta
#                             - same two, but targeting the tip of
#                                origin/main instead of the latest tag -
#                                the update screen's opt-in "beta updates"
#                                toggle, for deliberately running untagged
#                                work. Switching back off just returns to
#                                tag tracking: a Pi already past the latest
#                                tag stays put until a newer tag lands.
#   acom-update-apply rollback
#                             - for a Pi running a beta build (ahead of the
#                                latest release tag): moves back to that
#                                release, reinstalls requirements and
#                                restarts. The update screen offers this
#                                when beta updates are switched off. Uses
#                                `git reset --keep`, which refuses (and
#                                changes nothing) if a locally edited file
#                                - e.g. the Pi's own config.json - would be
#                                overwritten.
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
case "$MODE" in
  check|apply) BETA=0 ;;
  check-beta) BETA=1; MODE=check ;;
  beta) BETA=1; MODE=apply ;;
  rollback) BETA=0 ;;
  *) echo "acom-update-apply: unknown mode '$MODE'" >&2; exit 2 ;;
esac

echo "acom-update-apply: fetching tags"
git_as_owner fetch --tags --quiet origin

# The latest release tag, whichever mode we're in - by version sort, not by
# tag-creation date, so a tag pushed out of order can't confuse it. Only
# vX.Y.Z-shaped tags count; this repo shouldn't have other tag shapes, but
# don't assume.
RELEASE_TAG="$(git_as_owner tag --list 'v*' --sort=-v:refname | head -n1)"

# "yes" when HEAD is a beta build: strictly ahead of the latest release
# (the release is an ancestor, but HEAD isn't the release itself).
ahead_of_release() {
  [[ -n "$RELEASE_TAG" ]] \
    && git_as_owner merge-base --is-ancestor "$RELEASE_TAG" HEAD \
    && [[ "$(git_as_owner rev-parse HEAD)" != "$(git_as_owner rev-parse "$RELEASE_TAG^{commit}")" ]]
}

if [[ "$BETA" == 1 ]]; then
  # Tip of main, whatever's there - tagged or not. LATEST_REF is what gets
  # merged; LATEST_TAG doubles as the display string (describe-style, e.g.
  # "v0.1.2-4-gabc1234") so the rest of this script can stay shared.
  LATEST_REF="origin/main"
  LATEST_TAG="$(git_as_owner describe --tags --always "$LATEST_REF")"
else
  LATEST_TAG="$RELEASE_TAG"
  LATEST_REF="$LATEST_TAG"
fi
CURRENT_VERSION="$(git_as_owner describe --tags --always)"

if [[ "$MODE" == "check" ]]; then
  echo "current=$CURRENT_VERSION"
  echo "latest_ref=${LATEST_REF:-none}"
  echo "release=${RELEASE_TAG:-none}"
  if ahead_of_release; then
    echo "ahead_of_release=yes"
  else
    echo "ahead_of_release=no"
  fi
  if [[ -z "$LATEST_TAG" ]]; then
    echo "latest=none"
    echo "update_available=no"
    exit 0
  fi
  echo "latest=$LATEST_TAG"
  if git_as_owner merge-base --is-ancestor "$LATEST_REF" HEAD; then
    echo "update_available=no"
  else
    echo "update_available=yes"
  fi
  exit 0
fi

if [[ "$MODE" == "rollback" ]]; then
  if ! ahead_of_release; then
    echo "acom-update-apply: not running a beta build ($CURRENT_VERSION, latest release ${RELEASE_TAG:-none}) - nothing to roll back"
    exit 0
  fi
  echo "acom-update-apply: rolling back $CURRENT_VERSION -> $RELEASE_TAG"
  git_as_owner reset --keep "$RELEASE_TAG"
else
  if [[ -z "$LATEST_TAG" ]]; then
    echo "acom-update-apply: no tagged release exists yet (current: $CURRENT_VERSION) - nothing to update to"
    exit 0
  fi

  if git_as_owner merge-base --is-ancestor "$LATEST_REF" HEAD; then
    echo "acom-update-apply: already at or past $LATEST_TAG ($CURRENT_VERSION) - nothing to restart"
    exit 0
  fi

  echo "acom-update-apply: updating $CURRENT_VERSION -> $LATEST_TAG"
  git_as_owner merge --ff-only "$LATEST_REF"
fi

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
