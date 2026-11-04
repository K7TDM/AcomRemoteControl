# AcomRemoteControl

Remote control for ACOM S-series amplifiers (600S/1200S/2000S) and
04AT/06AT/20AT antenna tuners - a DIY, Raspberry Pi-based alternative to
ACOM's eBox Ethernet Remote Control accessory.

The Pi connects to the amp's rear-panel RS-232 port over a USB-to-RS232
adapter and speaks its serial protocol (reverse-engineered from ACOM's
own protocol spec - RS-232, 9600 baud 8N1, a simple length-prefixed,
checksummed frame format), hosting a local web dashboard for status and
control. See `PI_CONTROL_PLAN.md` for the full build plan.

## Status

Protocol research and build planning are done (see `PI_CONTROL_PLAN.md`).
The backend/frontend application now lives in `acom-control/` - see
`acom-control/README.md` for install/run instructions. It's been run
against real hardware across several sessions now; see
`PI_CONTROL_PLAN.md`'s "Status" section for what's confirmed vs. still
needs further real-hardware verification.

Releases are tagged (`vX.Y.Z`) - see `CHANGELOG.md` for what's in each
one. The dashboard's own update checker tracks these tags, not every
commit to `main`.

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
git clone git@github.com:K7TDM/AcomRemoteControl.git
cd AcomRemoteControl
bash install.sh
```

This repo is currently private, so whichever machine runs this clone
needs its own SSH key added to the repo's deploy keys (or your GitHub
account) first. A `github.com-k7tdm` SSH host alias is sometimes used
to pick a specific key for this repo on a given machine, but that alias
is only ever defined locally in `~/.ssh/config` - it won't resolve on a
machine that doesn't have the same config entry, including a fresh Pi.

This installs:
- **System packages** (via apt): `python3`, `python3-venv`, `python3-pip`,
  `git`, `avahi-daemon` (mDNS hostname), `usbutils` (for identifying the
  USB-serial adapter)
- Adds your user to the `dialout` group (required for serial port access)
- A Python virtual environment at `~/acom-control-venv` with the packages
  in `requirements.txt`: `fastapi`, `uvicorn[standard]`, `pyserial`,
  `pymodbus` (pinned to `3.6.9` - 3.7+ removed the datastore API this
  project's Modbus server uses)

After it finishes, log out and back in (or reboot) so the `dialout` group
membership takes effect.

Re-run `bash install.sh` any time `requirements.txt` changes.

## Running it

See `acom-control/README.md` for hardware wiring/udev setup and how to
run the backend (development command or as a systemd service). The web
dashboard, Setup wizard, and Antenna Setup screen are served from there
once it's running.

## Kiosk mode (dashboard on the Pi's own screen)

Plug a screen into the Pi - the official touchscreen or an HDMI
monitor - and install with `--kiosk` to show the dashboard on it full
screen, every time the Pi starts:

```bash
bash install.sh --kiosk
```

Or turn it on later without the command line: in the dashboard (from any
device), open **Settings -> Display & Sound -> Raspberry Pi display**.

What kiosk mode does:
- Starts a full-screen browser on the Pi's screen at every boot, once the
  dashboard is up. Works on Raspberry Pi OS Lite - no desktop needed
  (it installs `cage` and Chromium). Pi 4 or newer recommended.
- Behaves like an instrument panel: no cursor unless a mouse is used, no
  text selection, long-press menu or pinch zoom on the touchscreen.
- Dims the screen after 10 minutes idle; a touch or key-up wakes it.
  Brightness is set in Settings (kiosk only).
- Reloads itself after the dashboard is updated, so nobody has to touch it.
- Shows the Pi's own address at the bottom of the dashboard, so you can
  find it from another device without a static IP.

To get a console back, plug in a keyboard and press Ctrl+Alt+F2, or turn
it off in Settings (or `sudo acom-update-apply kiosk-off`). Details,
including Touch Display 2 setup, are in `acom-control/README.md`'s
"Kiosk display" section.

## Documentation

The `documentation/` folder has the manufacturer reference PDFs used for
protocol research:
- `ACOM A600S A1200S Serial Protocol V1.3.pdf` - the serial protocol spec
- `ACOM 1200S User Manual GB 08_2024.pdf`
- `ACOM 06AT_20AT User Manual GB 09_2025.pdf`
