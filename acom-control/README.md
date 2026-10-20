# acom-control

FastAPI backend + plain HTML/JS/CSS frontend for the ACOM Remote Control
project. See `../PI_CONTROL_PLAN.md` for the full design - this file is
just the install/run steps.

**Status:** has been run against real hardware across several sessions -
see `PI_CONTROL_PLAN.md`'s "Status" section for what's confirmed vs.
still needs further real-hardware verification. Releases are tagged
(`vX.Y.Z`); see `../CHANGELOG.md`.

## Install

From the repo root, `bash install.sh` sets up everything needed to run
this backend: system packages, a Python venv at `~/acom-control-venv`
(with this backend's dependencies already installed into it from the
repo-root `requirements.txt` - no separate `pip install` needed here),
the `/dev/acom-amp` udev rule (auto-detects the USB-RS232 adapter's serial
number if it's plugged in when you run the script), the `acom-control`
systemd service (installed, enabled, and started), and the self-update
helper (see "Updating" below). See the repo-root README for details. If
you haven't run it yet, do that first.

## Hardware setup

The amp's rear-panel RS-232 port runs at 9600 baud, 8N1. DB9 pinout:
pin 2 TxD, pin 3 RxD, pin 5 GND; pins 6 (DSR) and 8 (CTS) are both wired
to the amp's "Remote Power On" input (pins 1/4/7/9 not connected).

1. Plug in the USB-to-RS232 adapter and connect it to the amp's rear DB9
   RS-232 port.
2. If `install.sh` didn't find the adapter (it wasn't plugged in yet),
   either re-run it now that it is, or install the udev rule manually -
   see `setup/99-acom-amp.rules` for the serial-number lookup and install
   steps.
3. Confirm `/dev/acom-amp` exists (`ls -l /dev/acom-amp`) before starting
   the backend. If it's missing, the backend now starts anyway with comms
   down (rather than failing to boot) - pick a working port from the
   Setup screen's serial-port selector once it's up, or fix the udev rule
   and restart.
4. **Power toggle:** on real 1200S hardware, asserting DTR+RTS together
   for ~1.5s then releasing them reliably toggles the amp's power state
   (on→off or off→on, whichever it wasn't) - this is what the dashboard's
   Power On/Off buttons actually do; there's no separate RS-232 "power
   off" command (the amp's own software shutdown command hangs its
   internal sequence 100% of the time it's been tried, so the pulse
   replaces it entirely). Just *opening* the serial port (backend start/
   restart/crash-recovery) does **not** toggle power - it deliberately
   avoids driving DTR/RTS at all on open or close, only a real pulse does.

## Configure

Edit `config.json` (amp model, tuner model, ports) - or just run the app
and use the Setup screen (gear icon) in the web UI, which edits and saves
this file for you with no restart needed.

## Run (development)

```bash
cd acom-control
~/acom-control-venv/bin/uvicorn backend.app:app --host 0.0.0.0 --port 8000 --reload
```

Then open `http://<pi-ip-or-hostname>:8000/` in a browser.

## Run as a service (production)

`install.sh` already creates the `acom-control` system user, installs
`/etc/systemd/system/acom-control.service` (with `WorkingDirectory`/
`ExecStart` filled in for wherever it found this repo checked out), and
enables + starts it. To watch it or restart it later:

```bash
journalctl -u acom-control -f
sudo systemctl restart acom-control
```

(If the repo gets moved after `install.sh` ran, re-run it to regenerate
the unit file with the new paths, or edit `WorkingDirectory`/`ExecStart`
in `/etc/systemd/system/acom-control.service` directly.)

## Updating

The dashboard has its own update button (header icon) - click it to
check whether a newer tagged release (`vX.Y.Z`) is available, and
"Update Now" right there to pull + restart on demand. Ordinary commits
to `main` don't trigger this - only an actual `git tag vX.Y.Z && git
push --tags` does (see `../CHANGELOG.md`).

This works by granting the unprivileged `acom-control` service account
passwordless sudo for exactly one fixed, root-owned helper script
(`/usr/local/bin/acom-update-apply`, provisioned by `install.sh` - see
`setup/acom-control-update.sudoers` to review or revoke it). The manual
fallback, e.g. over SSH: `sudo acom-update-apply`.

## Logs

Backend and frontend-reported errors share one journald stream but use
distinct logger names (see `PI_CONTROL_PLAN.md`'s "Backend vs. frontend
logging"):

```bash
journalctl -u acom-control | grep '^acom.frontend'   # client-side errors
journalctl -u acom-control | grep '^acom.backend'    # serial/protocol/API
```

The Setup screen also has a debug-logging checkbox (verbose TX/RX frame
capture, off by default), a "Download Logs" button (exports the last
20000 `journalctl` lines as a file), and a "Clear Log" button - doesn't
delete anything from journald, just moves the start point of future
downloads forward so they don't keep growing to cover the service's
entire uptime.

## Modbus

A Modbus TCP server runs on port 5020 (configurable in `config.json`) for
3rd-party control (e.g. QSO Director) - telemetry + band/antenna cycling
only, see `backend/modbus_server.py`.
