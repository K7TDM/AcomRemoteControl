# acom-control

FastAPI backend + plain HTML/JS/CSS frontend for the ACOM Remote Control
project. See `../CLAUDE.md` for the serial protocol and `../PI_CONTROL_PLAN.md`
for the full design - this file is just the install/run steps.

**Status:** untested against real hardware - see `PI_CONTROL_PLAN.md`'s
"Status" section for exactly what still needs verification once a Pi + amp
are available.

## Install

From the repo root, `bash install.sh` already sets up the system packages
and a Python venv at `~/acom-control-venv` (see the repo-root README). If
you haven't run that yet, do it first.

Then install this backend's own dependencies into that venv:

```bash
~/acom-control-venv/bin/pip install -r backend/requirements.txt
```

## Hardware setup

1. Plug in the USB-to-RS232 adapter, connect it to the amp's rear DB9
   RS-232 port (see `../CLAUDE.md`'s pinout notes - verify the DSR vs CTS
   "Remote Power On" behavior empirically before relying on it).
2. Find the adapter's serial number and fill it into
   `setup/99-acom-amp.rules`, then install it:
   ```bash
   udevadm info --name=/dev/ttyUSB0 --attribute-walk | grep -m1 '{serial}'
   sudo cp setup/99-acom-amp.rules /etc/udev/rules.d/
   sudo udevadm control --reload-rules && sudo udevadm trigger
   ```
   This creates a stable `/dev/acom-amp` symlink regardless of which
   `/dev/ttyUSBx` the adapter enumerates as.
3. Confirm `/dev/acom-amp` exists (`ls -l /dev/acom-amp`) before starting
   the backend - `config.py` fails loudly at startup if it's missing.

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

```bash
sudo useradd --system --no-create-home --groups dialout acom-control
sudo cp setup/acom-control.service /etc/systemd/system/
# edit WorkingDirectory/ExecStart paths in the unit file to match where
# this repo is actually checked out, if different from the template
sudo systemctl daemon-reload
sudo systemctl enable --now acom-control
journalctl -u acom-control -f
```

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
