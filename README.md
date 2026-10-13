# AcomRemoteControl

Remote control for ACOM S-series amplifiers (600S/1200S/2000S) and
04AT/06AT/20AT antenna tuners - a DIY, Raspberry Pi-based alternative to
ACOM's eBox Ethernet Remote Control accessory.

The Pi connects to the amp's rear-panel RS-232 port over a USB-to-RS232
adapter, speaks the amp's serial protocol, and will host a local web
dashboard for status + control. See `CLAUDE.md` for the reverse-engineered
protocol details and `PI_CONTROL_PLAN.md` for the full build plan.

## Status

Protocol research and build planning are done (see `CLAUDE.md` /
`PI_CONTROL_PLAN.md`). Application code isn't written yet. `install.sh` /
`requirements.txt` set up the dependencies ahead of that so a Pi is ready
to go as soon as the backend/frontend land.

## Hardware

- Raspberry Pi (any model with a free USB port; Pi 4 recommended)
- A USB-to-RS232 adapter, connected from the Pi to the amp's rear DB9
  RS-232 port
- OS: **Raspberry Pi OS Lite (64-bit)**, headless - no desktop needed,
  everything is browser-based from another device on the LAN

## Installing dependencies

On a fresh Raspberry Pi OS Lite install, clone this repo and run the
installer:

```bash
git clone git@github.com-personal:tymaggert/AcomRemoteControl.git
cd AcomRemoteControl
bash install.sh
```

This installs:
- **System packages** (via apt): `python3`, `python3-venv`, `python3-pip`,
  `git`, `avahi-daemon` (mDNS hostname), `usbutils` (for identifying the
  USB-serial adapter)
- Adds your user to the `dialout` group (required for serial port access)
- A Python virtual environment at `~/acom-control-venv` with the packages
  in `requirements.txt`: `fastapi`, `uvicorn[standard]`, `pyserial`,
  `pymodbus`

After it finishes, log out and back in (or reboot) so the `dialout` group
membership takes effect.

Re-run `bash install.sh` any time `requirements.txt` changes.

## Documentation

The `documentation/` folder has the manufacturer reference PDFs used for
protocol research:
- `ACOM A600S A1200S Serial Protocol V1.3.pdf` - the serial protocol spec
- `ACOM 1200S User Manual GB 08_2024.pdf`
- `ACOM 06AT_20AT User Manual GB 09_2025.pdf`
