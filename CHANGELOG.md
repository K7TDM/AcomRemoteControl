 

# Changelog

All notable changes to this project are documented here. Versions are
git tags (`vX.Y.Z`, [Semantic Versioning](https://semver.org/)), not raw
commits - the dashboard's update checker (and the `acom-update-apply`
helper behind it) compares against the latest tag, so ordinary commits
pushed to `main` don't trigger an "update available" notice. Only a
deliberate release does:

```bash
git tag v0.2.0
git push --tags
```

## [Unreleased]

Changes committed to `main` but not yet tagged as a release land here
first, then get moved under a version heading below when tagged.

### Fixed
- Header update icon rendered as Apple's own colorful emoji glyph on
  iOS ("an arrow with a blue box") instead of the plain styleable
  glyph the `.has-update` amber highlight/bounce assumed, since a bare
  Unicode character's presentation is up to the OS, not CSS. Replaced
  with an inline SVG, which always renders exactly as styled with no
  OS-level substitution possible.

## [0.1.4] - 2026-10-20

### Fixed
- A stale cached `app.js` running against a fresh `index.html` (or
  vice versa) after an update failed completely silently - no visible
  error, buttons just stopped doing anything. `index.html`'s asset
  URLs (`app.js`, `styles/*.css`) are now stamped with the current
  version (e.g. `app.js?v=v0.1.4`) by a new `GET /` route, so every
  release gets URLs the browser has never cached before - nothing can
  go stale. **This release itself still needs one manual browser
  cache clear to take effect** (it can't fix the staleness of the
  version that doesn't have the fix yet) - every release after this
  one should refresh on its own without that.

## [0.1.3] - 2026-10-20

### Added

- The update icon now opens a full screen (same pattern as the Setup
  gear icon) instead of a small popover - current/new version, "Update
  Now," and a "View full changelog" link that opens this file in a new
  tab.
- When a newer release is found, the update screen shows exactly
  what's new - every changelog entry between the version you're on and
  the new one, pulled from `CHANGELOG.md` as of the new release's tag
  (not the working tree, which hasn't been updated yet).

## [0.1.2] - 2026-10-20

### Added

- The header update icon now highlights (amber) and gives a small,
  infrequent bounce on its own once the daily background check finds
  a newer tagged release - no click needed to notice. Re-added `GET /api/update/status` (cheap, cached - no git calls) for the frontend
  to poll every 10 minutes to drive this.

## [0.1.1] - 2026-10-20

### Fixed

- "Update Now" button is hidden until a check actually confirms a
  newer release is available, instead of always being clickable.
- Front-panel power-off could take up to ~20s to show up in the
  dashboard (two chained 10s grace periods, comms-watchdog detection
  plus an "assume off" timeout) - both lowered to 3s, cutting
  worst-case latency to ~6-7s.

### Changed

- READMEs no longer point to `CLAUDE.md` (being retired before this
  goes public) - pinout, the power-toggle behavior, and protocol
  basics are inlined directly instead. Also documented the debug-
  logging/log-download/clear and self-update features, which weren't
  mentioned in either README before.

## [0.1.0] - 2026-10-20

First tagged release. Still pre-1.0: several items are implemented but
marked "NEEDS REAL-HARDWARE VERIFICATION" in `CLAUDE.md` - see that file
for the full list.

### Added

- Web dashboard for the ACOM 1200S (FastAPI + pyserial backend, vanilla
  JS/HTML frontend) with ACOM/Classic/Modern themes and light/dark mode.
- Power on/off (DTR/RTS pulse, since the amp's own software "turn off"
  command hangs its shutdown sequence on real hardware), OPR/STB toggle,
  ATU Bypass/Full Tune, band/antenna cycling, buzzer test.
- Live telemetry: power/SWR/voltage/current gauges, fault code decoding,
  comms/power-state tracking distinct from raw link health.
- Setup screen: amp/tuner model selection (with auto-detection from the
  amp's own `0x11`/`0x16` replies, manual fallback), serial port
  selection, Antenna Setup matrix (names/types/band assignments), ATU
  memory erase tools.
- Debug logging toggle and log download (`journalctl`-backed) with a
  "Clear Log" marker so downloads stay focused on recent activity
  instead of a service's entire uptime.
- Self-update: a header button checks for a new tagged release on
  click (plus once a day in the background) and can pull + restart on
  demand - the feature this versioning scheme exists to support.
- Modbus TCP bridge exposing amp telemetry/controls as registers/coils.
