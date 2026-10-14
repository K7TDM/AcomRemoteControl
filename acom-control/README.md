# acom-control

FastAPI backend + plain HTML/JS/CSS frontend for the ACOM Remote Control
project. See `../CLAUDE.md` for the serial protocol and `../PI_CONTROL_PLAN.md`
for the full design - this file is just the install/run steps.

**Status:** untested against real hardware - see `PI_CONTROL_PLAN.md`'s
"Status" section for exactly what still needs verification once a Pi + amp
are available.

## Install

From the repo root, `bash install.sh` sets up everything needed to run
this backend: system packages, a Python venv at `~/acom-control-venv`
(with this backend's dependencies already installed into it from the
repo-root `requirements.txt` - no separate `pip install` needed here),
the `/dev/acom-amp` udev rule (auto-detects the USB-RS232 adapter's serial
number if it's plugged in when you run the script), and the
`acom-control` systemd service file (installed but not started). See the
repo-root README for details. If you haven't run it yet, do that first.

## Hardware setup

1. Plug in the USB-to-RS232 adapter, connect it to the amp's rear DB9
   RS-232 port (see `../CLAUDE.md`'s pinout notes - verify the DSR vs CTS
   "Remote Power On" behavior empirically before relying on it).
2. If `install.sh` didn't find the adapter (it wasn't plugged in yet),
   either re-run it now that it is, or install the udev rule manually -
   see `setup/99-acom-amp.rules` for the serial-number lookup and install
   steps.
3. Confirm `/dev/acom-amp` exists (`ls -l /dev/acom-amp`) before starting
   the backend. If it's missing, the backend now starts anyway with comms
   down (rather than failing to boot) - pick a working port from the
   Setup screen's serial-port selector once it's up, or fix the udev rule
   and restart.
4. **DTR/RTS power-toggle risk:** opening the serial port - which happens
   on every backend start, restart, or crash-recovery, including the
   systemd service starting - raises DTR, and on real 1200S hardware a
   DTR pulse has been observed to toggle the amp's power state (see
   `../CLAUDE.md` open question 4). There's no software fix; if this is
   unacceptable, don't wire DB9 pins 6/8 in the Pi-to-amp cable at all.

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

`install.sh` already creates the `acom-control` system user and installs
`/etc/systemd/system/acom-control.service` (with `WorkingDirectory`/
`ExecStart` filled in for wherever it found this repo checked out) - it
deliberately doesn't start or enable it, because of the DTR power-toggle
risk noted above. Once you've read that and are ready:

```bash
sudo systemctl enable --now acom-control
journalctl -u acom-control -f
```

(If the repo gets moved after `install.sh` ran, re-run it to regenerate
the unit file with the new paths, or edit `WorkingDirectory`/`ExecStart`
in `/etc/systemd/system/acom-control.service` directly.)

## Logs

Backend and frontend-reported errors share one journald stream but use
distinct logger names (see `PI_CONTROL_PLAN.md`'s "Backend vs. frontend
logging"):

```bash
journalctl -u acom-control | grep '^acom.frontend'   # client-side errors
journalctl -u acom-control | grep '^acom.backend'    # serial/protocol/API
```

## Modbus

A Modbus TCP server runs on port 5020 (configurable in `config.json`) for
3rd-party control (e.g. QSO Director) - telemetry + band/antenna cycling
only, see `backend/modbus_server.py`.
