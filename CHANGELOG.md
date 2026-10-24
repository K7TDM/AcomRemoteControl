 

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

### Changed
- The OPR/STB button now says what it will do: "Standby" (green) while
  in Operate, "Operate" (yellow, was red) while in Standby.

## [0.1.6] - 2026-10-23

### Added
- Setup screen "Display" section: show/hide each dashboard gauge and the
  band panel, set each to quarter, half or full width (the band panel is
  always full), and drag them into any order with the grab handle (works
  on a phone too). Saved on the Pi, so every device shows the same
  layout. "Reset to Default" restores the original layout.
- Forward Power shows a yellow marker at the average of the last
  transmission, next to the red peak marker.

### Changed
- ATU Bypass button: leaving Bypass now re-sends the Bypass command as
  a toggle (like holding TUNE on the amp's front panel), which recalls
  the stored tuning, instead of always running a full tune. If the amp
  stays in Bypass, the dashboard says so - use Tune to re-engage.
- The ATU button now says what it will do: "ATU Enable" (yellow) while
  bypassed, "ATU Bypass" (green) otherwise, and "Tuning…" while a tune
  is waiting for RF.
- Peak and average now cover the whole transmission: TX drops shorter
  than 1.5s (pauses within one over) no longer reset them.
- Removed the "◀ Band" / "Band ▶" buttons from the dashboard - the
  radio's CAT link already sets the band.

### Fixed
- After an update or service restart the dashboard could look
  disconnected (no mode, power or alarms) even though the amp was
  talking, and power-cycling didn't help. The startup sequence was
  switching the amp to its "ATU Measure" data stream; it now asks for
  the normal one, and switches the amp back if it's found in that state.
- Buzzer Test reported an error even though the buzzer sounded (and
  could beep several times) - the amp never acknowledges it.
- The warning banner kept showing a fault ("Unknown fault 0x8E") long
  after the amp had cleared it from its own screen. The banner now
  shows the alarm the amp is displaying right now.
- After a successful tune the banner showed "Unknown fault 0x8E". Code
  `0x8E` (not in ACOM's protocol doc) is now named "TUNED SWR < 1.3",
  matching the amp's own screen. `0x14`, seen once on a low-drive tune,
  reads "ATU TUNING NOTICE".

## [0.1.5] - 2026-10-23

### Added
- Setup screen's new "CAT / Rig Control" section shows and lets you
  change the amp's own rear-panel CAT port config (interface type,
  command set/"library", baud rate, byte spacing, polling time) - same
  settings as the amp's own front-panel CAT menu, read via message
  `0x12` and written via `0x05`/`0x06`/`0x07`.
- "Download Amp Snapshot" button (Setup screen) re-syncs and downloads
  every piece of amp state this backend knows how to ask for as one
  JSON file - for comparing real-hardware setups over time (different
  radio, with/without a tuner, etc.), not a single-field lookup. Now
  includes the full per-antenna/per-band ATU tuning matrix (44 combos)
  and all four Hard Fault event records, neither of which existed
  before - see `CLAUDE.md` open question 15.
- "Beta updates" toggle on the update screen: when on, the updater
  offers any new commit on `main`, not just tagged releases, and
  "What's new" lists the new commits instead of changelog entries.
  **Needs `install.sh` re-run once on the Pi** to install the updater's
  new modes and their sudo permission.
- "Check for Updates" button on the update screen, to re-run the check
  on demand.
- Band panel on the dashboard, modeled on the amp's own Basic Screen:
  ATU status, current band/segment edges, band name and antenna (e.g.
  "A1R"), over a scale across the whole band with the amp's current
  tuning segment highlighted, and a marker at the carrier frequency
  when it falls inside the band. Band edges follow the ARRL US amateur
  band chart. An optional CB band (26.965 - 27.405 MHz) can be turned
  on per browser from the Setup screen.

### Changed
- Setup screen sections are collapsible and start closed.
- Input Power and PA DC Current now have the same metered bargraph
  (amber header bar in the ACOM theme) as the other gauges, instead of
  bare numbers.
- The big carrier-frequency readout is replaced by the band panel above.

### Fixed
- Restarting the service (e.g. re-running `install.sh`, or a
  self-update) could leave the amp locked up. Linux turns on DTR/RTS -
  the amp's "Remote Power On" lines - every time the serial port is
  opened, and nothing turned them back off. They're now released right
  after every open, at the end of every power pulse (even an
  interrupted one), and at shutdown.
- After an update the browser could keep showing the old dashboard
  until a hard refresh. On the Pi, every git command the service ran
  failed (the repo belongs to a different user than the service, which
  git refuses by default), so the version stamped on the dashboard's
  file URLs was always "unknown" and never changed between updates.
  The service's git calls now trust just this one repo explicitly.
  The same failure also hid the version on the update screen and
  stopped "What's new" from loading. **This release itself still needs
  one hard refresh** (the version running before it can't stamp new
  URLs) - later updates should refresh on their own.
- The dashboard's antenna number was one lower than the amp's own
  display (showed 3 for "A4"): the amp counts antennas from 0 on the
  wire, contrary to the protocol doc. Antenna numbers now match the
  amp's display, and commands that take an antenna number send the
  0-based value.
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
