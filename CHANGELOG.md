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
