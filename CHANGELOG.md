 

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

### Added
- A small line at the bottom of the dashboard shows the Pi's own address
  (e.g. `http://10.10.50.254:8000`), so a Pi without a static IP can be
  found from the kiosk screen. One address even with Ethernet and Wi-Fi
  both up (the one the default route uses), refreshed every minute in
  case the DHCP lease changes.
  New `GET /api/network` endpoint.
- Power sounds alongside the alarm sounds, in the amp's own Morse: TEST
  when it powers up, R once it's ready (STB/OPR), and AR (.-.-.) when it
  powers off. The manual doesn't spell these out - they're as heard from
  the amp. Same on/off switch and volume as the alarm sounds.

### Changed
- Settings re-reads everything from the amp each time it opens, so the
  "Refresh from Amp" button is gone. Anything you've started editing
  is left alone while the fresh values come in.
- Save buttons now live in the sections that need them: Amplifier &
  Tuner has its own Save, and Antenna Setup has Save Antennas. Only
  Close stays at the bottom.
- Download Amp Snapshot moved into Amplifier & Tuner, with a note that
  it's for debugging.
- Pressing the gear icon while Settings is open closes it again.
- Morse sounds (alarms and power) play at 20 WPM instead of 15, closer
  to the amp's own speed. Buzzer Test's "K7TDM TEST" on the amp is paced
  for 20 WPM too.
- Settings' Display section is now "Display & Sound", since it also
  holds the sound settings.
- The SWR gauge uses the usual SWR-meter scale: 1:1 to infinity, spaced by
  reflection coefficient so 3:1 sits in the middle and the high values
  crowd toward the infinity end. Red from 3:1 (amber from 2:1), in every
  theme. Modern's arc dials gained a thin red arc over their red range,
  like ACOM's red segments and Classic's redline.
- Software Update shows a "Checking for updates..." progress bar while
  the check runs, instead of half-filled info (no version yet, beta box
  looking unticked). The version, beta switch and buttons appear once
  the answer is in.

## [0.1.15] - 2026-10-29

### Added
- Alarm sounds in the browser: when the amp shows a new alarm, the
  dashboard plays it in Morse like the amp's buzzer - W for a warning, S
  for a soft fault, a series of F for a hard fault (the manual only
  names the hard-fault sound; W matches what the amp played for "ATU
  tuning outdated"; S is a stand-in). A successful tune stays silent.
  Each alarm code's level comes from the protocol doc's own WARNING /
  SOFT FAULT / HARD FAULT labels. On/off and a volume slider per browser
  in Settings -> Display, with a Test Sounds button (letting go of the
  slider plays a W at the new level); a new alarm also wakes a dimmed
  screen.

### Changed
- Power Off moved off the dashboard's button bar to the top of Settings,
  so it can't be hit by accident while operating. It's greyed out while
  the amp isn't on. This also stops the Classic theme's buttons getting
  cut off on the Pi's 800x480 touchscreen.
- Settings -> Display's brightness slider is now "Kiosk brightness" and
  only shows on the kiosk screen itself, so a phone or desktop browser
  can't change the Pi screen's brightness by accident.
- Classic theme: much larger numbers, unit and reading on the meters -
  the scale numbers were about 6px tall on the Pi touchscreen. The end
  numbers on the small meters no longer sit on the red zone.

## [0.1.14] - 2026-10-29

### Added
- Screen dimming: after 10 minutes with no touch and no key-up, the
  dashboard dims to 5% - the Pi touchscreen's real backlight on the
  kiosk, the page itself in a browser or on an HDMI monitor. A touch or
  key-up brings it back; the waking touch doesn't press the button under
  it.
- Setup -> Display: a "Screen brightness" slider for the Pi touchscreen,
  saved on the Pi. Needs the service in the `video` group, which the
  updated service file adds.

### Fixed
- The kiosk screen showed a mouse arrow with no mouse attached: the Pi's
  HDMI-CEC input devices (TV-remote buttons) are tagged as pointing
  sticks, so cage, the kiosk's display server, counted them as a mouse
  and drew its arrow. A udev rule now has libinput ignore them, so the
  arrow only appears with a real mouse, and the page hides it again
  after a few seconds without movement. Takes effect on the next reboot
  after updating (or `sudo systemctl restart acom-kiosk`). Replaces an
  earlier invisible-cursor-theme attempt, which cage never read.
- Power On could leave the dashboard stuck refusing Power On ("power
  state is unknown", 409) until the service was restarted: if the power
  pulse got no reply (amp unplugged, mains off), nothing ever resolved
  the state. It now falls back to "off" after 15s with no reply, so
  Power On can be tried again.
- After a fresh install on a new SD card the dashboard never started
  (the service crash-looped with `status=200/CHDIR`): current Raspberry
  Pi OS locks home folders to their owner, so the `acom-control` service
  account couldn't reach the app or its venv. `install.sh` now opens the
  home folder for traversal and makes `config.json` writable by the
  service, so Setup-screen changes save too.
- Buzzer Test only played the "K" of "K7TDM TEST": the amp plays one
  Morse character at a time and ignores any sent while it's busy. The
  Pi now times the characters itself at 15 WPM (about 7.5 seconds for
  the whole message); pressing the button again while it plays does
  nothing.

## [0.1.13] - 2026-10-27

### Changed
- Buzzer Test now plays "K7TDM TEST" in Morse on the amp's buzzer
  instead of a single beep. The amp plays each character with its own
  Morse table and timing.

## [0.1.12] - 2026-10-27

### Fixed
- Antenna names (and the other settings read from the amp at startup)
  often never arrived: the amp drops most requests sent back to back.
  They're now asked for one at a time, waiting for each answer and
  retrying. "Refresh from Amp" shows "Refreshing…" while it runs (a few
  seconds) and then shows what came back.
- The warning banner said "AMP-ATU COMMUNICATION ERROR" when the amp was
  showing "ATU tuning outdated - you may refresh it". That alarm code
  means "tuning outdated" on this amp, and is now named that.

## [0.1.11] - 2026-10-27

### Added
- The band panel shows the antenna's name after its number, e.g.
  "A1R - DXC", using the names set in Setup -> Antenna Setup. Antennas
  without a name still show just "A1R".

### Fixed
- Antenna Setup's Save reported "amplifier did not confirm antenna
  names" even though the amp took them, and so never sent the band
  assignments. The amp confirms by sending the names back rather than
  an OK; that now counts.
- Antenna Setup showed the wrong bands for each antenna (and would have
  written them wrong): the amp's band bytes are low byte first. An
  antenna set to every band now shows every band.

## [0.1.10] - 2026-10-27

### Added
- Kiosk display: show the dashboard full screen on a screen plugged into
  the Pi (official touchscreen or HDMI monitor), starting at every boot -
  works on Pi OS Lite, no desktop needed. Turn it on or off from Setup ->
  Display -> "Raspberry Pi display" (which also shows whether a display
  is connected), or with `bash install.sh --kiosk`. The cursor stays hidden until a mouse is used; touch never
  shows it. See "Kiosk display" in `acom-control/README.md`, including
  the setup line Touch Display 2 needs. Not yet tested on a real Pi.
- Open dashboards (including the kiosk screen) reload themselves when
  the backend comes back on a new version after an update, so they never
  keep running the old dashboard code.

## [0.1.9] - 2026-10-27

### Changed
- Updates (and rollbacks) now reinstall the updater and its sudo
  permission from the new code, so a release that changes them no longer
  needs `install.sh` re-run. A new sudo rule that fails validation is
  never installed - the old one stays. Takes effect from the first update
  run by an updater that has this.

### Fixed
- Two update checks running at the same moment (e.g. opening the update
  screen while another check is still going) could fail with "cannot
  lock ref". Checks now run one at a time.

## [0.1.8] - 2026-10-27

### Added
- Full-screen layout for landscape screens - monitors (16:9, 4:3, 5:4),
  the Raspberry Pi touchscreens (800x480 and 1280x720) and phones on
  their side. The dashboard fills the whole window with no scrolling:
  every gauge scales to its space, the status lights move up into the
  header, the buttons stay on one row, and on screens 3:2 or wider the
  gauges pack two layout-widths side by side. It's automatic - it
  follows the screen's size and orientation, nothing to set. Portrait
  screens keep the scrolling layout. In the ACOM and Modern themes the
  bar panels scale their bar and text with the panel, and stop growing
  at about 200px tall so they don't turn into tall, mostly empty boxes
  on a big monitor.
- Two more gauge sizes in Setup -> Display: ⅛ and "2× Full" (spans the
  whole screen on wide screens; the same as Full elsewhere).
- Bigger touch targets for the buttons on touchscreens.
- Update screen: with beta updates off while running a beta build, a
  "Roll back to vX.Y.Z" button returns to the latest release (keeps the
  Pi's own settings; refuses rather than overwrite any other local
  change). **Needs `install.sh` re-run once on the Pi** to install the
  updater's new rollback mode and its sudo permission.
- Update screen: when there's nothing newer, it shows just the current
  version and "Already up to date" instead of repeating the same
  version as "New version".

### Changed
- Modern theme redesigned as a "glass cockpit" (avionics display): navy
  panels with cyan accents in dark mode, a matching daylight palette in
  light mode. Forward Power is a smooth green-amber-red tape with a
  boxed digital readout, a magenta peak bug and a dashed cyan average;
  the other gauges are arc dials with the reading in a boxed window.
  Monospace instrument lettering and outlined soft-key buttons colored
  by state.
- Classic theme status row (Comms, Mode, ATU, Antenna): dark capped
  labels like the buttons, with solid jewel lamps that light green when
  the status is good - the amber lamps used to disappear into the amber
  labels.

### Fixed
- After an update the browser could run the new dashboard code with an
  old cached copy of its gauge or theme code (only the main script was
  version-stamped). All of them now update together.

## [0.1.7] - 2026-10-27

### Added
- Captions under the header controls: Theme, Light/Dark, Updates and
  Settings.
- A favicon (an "A" in ACOM amber), the same in every theme.

### Changed
- The OPR/STB button now says what it will do: "Standby" (green) while
  in Operate, "Operate" (yellow, was red) while in Standby.
- Classic theme redesigned as vintage station gear. Light mode is a
  Heath-style gray-green panel with cream backlit meters; dark mode is
  crinkle black with chrome-rimmed amber meters. Every gauge, including
  Forward Power, is now a rectangular backlit panel meter with a printed
  scale, red line, peak and average marks, and the reading on its face.
  The band dial is a backlit slide-rule window, and state buttons show
  a solid jewel lamp (green, amber or red) instead of a colored fill.
  Full-width gauges use a wide, short edgewise meter (styled like the
  band dial, with a flat needle sweeping across it) instead of a
  scaled-up round meter; half and quarter widths keep the standard one.

### Fixed
- Classic theme needles could swing off the dial and disappear: the
  needle was rotated about two different pivots at once.

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
