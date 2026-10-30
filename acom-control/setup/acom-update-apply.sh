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
# Seven modes, all invoked with a fixed, no-extra-argument command line so
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
#   acom-update-apply kiosk-on / kiosk-off
#                             - turn the kiosk display (the dashboard full
#                                screen on the Pi's own screen) on or off -
#                                Setup -> Display's "Raspberry Pi display"
#                                switch, and `install.sh --kiosk`. kiosk-on
#                                installs cage and Chromium first if
#                                they're missing (a few minutes the first
#                                time). Neither touches git.
#
# apply, beta and rollback also reinstall this script and its sudoers rule
# from the newly checked-out code (see the end of this file), so an update
# that changes them doesn't need install.sh re-run.
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
  kiosk-on|kiosk-off) ;;
  *) echo "acom-update-apply: unknown mode '$MODE'" >&2; exit 2 ;;
esac

# Keeps the Pi's HDMI-CEC devices from counting as a mouse in the kiosk (see
# 99-acom-kiosk-input.rules). Takes effect when cage next starts.
install_kiosk_input_rule() {
  install -o root -g root -m 644 "$REPO_DIR/acom-control/setup/99-acom-kiosk-input.rules" \
    /etc/udev/rules.d/99-acom-kiosk-input.rules
  udevadm control --reload-rules
  udevadm trigger --subsystem-match=input --action=change
}

# --- Kiosk display on/off (no git involved) ---------------------------------
if [[ "$MODE" == "kiosk-on" ]]; then
  echo "acom-update-apply: turning the kiosk display on"
  export DEBIAN_FRONTEND=noninteractive
  # cage: a single-app Wayland display server, so Pi OS Lite needs no
  # desktop. Chromium is "chromium" on current Raspberry Pi OS and
  # "chromium-browser" on older releases.
  if ! command -v cage >/dev/null || ! command -v curl >/dev/null \
     || ! { command -v chromium || command -v chromium-browser; } >/dev/null; then
    echo "acom-update-apply: installing cage and Chromium (a few minutes the first time)"
    apt-get update
    apt-get install -y cage curl
    if ! apt-get install -y chromium; then
      apt-get install -y chromium-browser
    fi
  fi
  # Display, GPU and input-device access for the kiosk session.
  for group in video render input; do
    if getent group "$group" >/dev/null; then
      usermod -aG "$group" "$REPO_OWNER"
    fi
  done
  install -o root -g root -m 755 "$REPO_DIR/acom-control/setup/acom-kiosk.sh" /usr/local/bin/acom-kiosk
  install_kiosk_input_rule
  PH="__"  # see the self-refresh step at the end of this file for why
  sed "s#${PH}KIOSK_USER${PH}#$REPO_OWNER#" "$REPO_DIR/acom-control/setup/acom-kiosk.service" \
    > /etc/systemd/system/acom-kiosk.service
  systemctl daemon-reload
  systemctl enable acom-kiosk
  systemctl restart acom-kiosk
  echo "acom-update-apply: kiosk display on"
  exit 0
fi

if [[ "$MODE" == "kiosk-off" ]]; then
  echo "acom-update-apply: turning the kiosk display off"
  systemctl disable --now acom-kiosk 2>/dev/null || true
  # Give the console its login prompt back right away, not just next boot.
  systemctl start getty@tty1 2>/dev/null || true
  echo "acom-update-apply: kiosk display off"
  exit 0
fi

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

# Reinstall this helper and its sudo rule from the code just checked out,
# the same way install.sh does, so changes to them (a new mode like
# rollback) take effect without re-running install.sh. Before this, the
# installed copy only ever changed when install.sh was re-run - and running
# it before the update that carried the change installed the old copy.
#
# Both are written to a temp file on the same filesystem and renamed into
# place: bash reads a running script incrementally, so overwriting this
# file in place could corrupt the rest of this run (the rename leaves this
# run on the old copy), and sudo ignores files in sudoers.d whose names
# contain a "." until the rename. A new sudo rule that fails visudo's check
# is never installed - the old one stays and the update carries on.
echo "acom-update-apply: refreshing this updater and its sudo rule"
SELF_SRC="$REPO_DIR/acom-control/setup/acom-update-apply.sh"
SELF_DST="/usr/local/bin/acom-update-apply"
SELF_TMP="$(mktemp /usr/local/bin/.acom-update-apply.XXXXXX)"
# The placeholder names are assembled from $PH rather than written out:
# install.sh (and this very step) substitute every literal placeholder in
# this file, which would also rewrite the patterns below into no-ops and
# leave the next installed copy with its placeholders unfilled.
PH="__"
sed \
  -e "s#${PH}REPO_DIR${PH}#$REPO_DIR#" \
  -e "s#${PH}REPO_OWNER${PH}#$REPO_OWNER#" \
  -e "s#${PH}VENV_DIR${PH}#$VENV_DIR#" \
  "$SELF_SRC" > "$SELF_TMP"
chown root:root "$SELF_TMP"
chmod 755 "$SELF_TMP"
mv -f "$SELF_TMP" "$SELF_DST"

SUDOERS_SRC="$REPO_DIR/acom-control/setup/acom-control-update.sudoers"
SUDOERS_DST="/etc/sudoers.d/acom-control-update"
SUDOERS_TMP="/etc/sudoers.d/.acom-control-update.new"
install -o root -g root -m 440 "$SUDOERS_SRC" "$SUDOERS_TMP"
if visudo -cf "$SUDOERS_TMP" >/dev/null; then
  mv -f "$SUDOERS_TMP" "$SUDOERS_DST"
else
  rm -f "$SUDOERS_TMP"
  echo "acom-update-apply: WARNING - the new sudo rule failed validation; keeping the old one" >&2
fi

# Kiosk display (install.sh --kiosk), only if it's installed: refresh its
# launcher and unit the same way. The running kiosk isn't restarted - the
# dashboard page reloads itself when the backend comes back on a new
# version (app.js), and launcher/unit changes apply from the next boot.
KIOSK_UNIT="/etc/systemd/system/acom-kiosk.service"
if [[ -f /usr/local/bin/acom-kiosk && -f "$KIOSK_UNIT" ]]; then
  echo "acom-update-apply: refreshing the kiosk display launcher"
  KIOSK_TMP="$(mktemp /usr/local/bin/.acom-kiosk.XXXXXX)"
  cp "$REPO_DIR/acom-control/setup/acom-kiosk.sh" "$KIOSK_TMP"
  chown root:root "$KIOSK_TMP"
  chmod 755 "$KIOSK_TMP"
  mv -f "$KIOSK_TMP" /usr/local/bin/acom-kiosk
  sed "s#${PH}KIOSK_USER${PH}#$REPO_OWNER#" "$REPO_DIR/acom-control/setup/acom-kiosk.service" > "$KIOSK_UNIT"
  systemctl daemon-reload
  install_kiosk_input_rule
fi

echo "acom-update-apply: restarting acom-control"
systemctl restart --no-block acom-control
