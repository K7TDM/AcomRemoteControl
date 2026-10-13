# ACOM 1200S Raspberry Pi Web Control — Implementation Plan

## Context

The user owns an ACOM 1200S HF amplifier and an ACOM 06AT/20AT antenna
tuner/switch, and wants to build their own Raspberry Pi–based remote control
instead of buying ACOM's eBox accessory. The Pi will sit on the amp's rear
RS-232 port, speak the documented ACOM serial protocol, and host a web
dashboard on the local network for full control + live telemetry.

Protocol research is already captured in `CLAUDE.md` (same folder) from the
`ACOM A600S A1200S Serial Protocol V1.3.pdf` doc: 9600 8N1 framing,
`0x55/addr/len/data/checksum` message structure, ACK-on-receipt via message
`0x86`, the `0x81` command envelope with its sub-commands (mode change,
band/antenna cycling, ATU management, buzzer, etc.), and the main cyclic
telemetry message `0x2F` (power, SWR, voltages, temps, fault flags).

Known gap: the 06AT/20AT tuner may not be covered by the same protocol doc
(it only defines ATU model `04AT` / selector `10AS`). Per the user's
decision, this build will treat the tuner as **status passthrough only** —
display whatever ATU/ASEL fields come through — and defer active tuner
commands until that protocol gap is resolved.

Decisions already made with the user:
- **Backend:** Python + FastAPI + `pyserial`
- **Live updates:** WebSocket push of parsed telemetry to the browser
- **Scope v1:** Full amp control (power, mode, band/antenna, buzzer,
  telemetry dashboard) using the known protocol; ATU/ASEL shown as
  read-only status, no tuner commands sent yet
- **Hardware link:** USB-to-RS232 adapter from the Pi to the amp's DB9
- **Amp models supported:** 600S / 1200S / 2000S, selected in config
  (telemetry parsing adjusts per-model — see Config section)
- **Tuner models supported:** 04AT / 06AT / 20AT, selected in config as an
  informational/capability flag (tuner is relayed through the amp's single
  RS-232 link, it has no serial port of its own)
- **3rd-party control:** a Modbus TCP server exposing amp status/commands,
  so external apps (e.g. QSO Director) can control the amp alongside the
  web UI
- **Auth:** none for v1 — the web UI, REST/WebSocket API, and Modbus server
  all trust the local network
- **Serial device identification:** a stable udev symlink keyed to the
  USB-serial adapter's serial number (not a raw `/dev/ttyUSB0` path, which
  can renumber across reboots or if another USB-serial device is plugged in)

## Pi OS & System Setup

- **OS**: Raspberry Pi OS Lite, 64-bit (current Debian "Bookworm" base) —
  headless, no desktop needed since everything is browser-based from
  another device on the LAN. Any Pi with a free USB port has plenty of
  headroom for this workload; wired Ethernet is preferable to Wi-Fi for a
  control link, but Wi-Fi is fine on a stable home network.
- **APT packages**: `python3-venv` (Python 3.11+ ships with Bookworm),
  `git`, `avahi-daemon` (mDNS `acom.local` hostname), `usbutils`
  (`lsusb`/`udevadm info`, needed once to find the USB-serial adapter's
  serial number for the udev rule). Optionally `ufw`, allowing only SSH +
  the web port + the Modbus port — complements the earlier "no auth,
  trust the LAN" decision by at least narrowing what's reachable.
- **Python packages** (installed in a venv, not system-wide):
  `fastapi`, `uvicorn[standard]` (the `[standard]` extra pulls in
  websocket/httptools support the `/ws/status` endpoint needs),
  `pyserial`, `pymodbus`.
- **Modbus port**: use a non-privileged port (**5020**, not the standard
  502) so the service never needs root or `CAP_NET_BIND_SERVICE` — update
  `config.json`'s `modbus.port` accordingly, and set the Modbus port to
  5020 in whatever 3rd-party client (QSO Director etc.) connects to it.
- **Service account**: a dedicated unprivileged user for the systemd
  service, added to the **`dialout`** group (required for serial port
  access on Linux — the default `pi` user already has it, a dedicated
  service user needs it added explicitly).
- **Logging**: log to stdout and let `journald` handle rotation
  (`journalctl -u acom-control`, size cap via `journald.conf`) instead of
  a hand-rolled log file — simpler than custom rotation, drops the need
  for a `logging.path` config field (keep just `logging.level`).
  **Two separate, traceable logger names** so a frontend failure doesn't
  get buried in backend serial/protocol noise (and vice versa) — see
  "Backend vs. frontend logging" below.
- **Networking**: a DHCP reservation on the router for the Pi's IP,
  rather than a static-IP config on the Pi itself — easier to manage in
  one place, and the Modbus client generally wants a fixed IP anyway.
  `avahi-daemon` gives the web UI a friendly `acom.local` hostname for
  browser use on top of that.

## Config

A single `config.json` (loaded by `config.py`) drives everything that
differs between installs or hardware:

```json
{
  "amp": {
    "model": "1200S",            // "600S" | "1200S" | "2000S"
    "serial_device": "/dev/acom-amp"   // stable udev symlink, see below
  },
  "tuner": {
    "model": "06AT"              // "04AT" | "06AT" | "20AT" | null (none attached)
  },
  "web": {
    "host": "0.0.0.0",
    "port": 8000
  },
  "modbus": {
    "enabled": true,
    "host": "0.0.0.0",
    "port": 5020               // non-privileged port, see Pi OS & System Setup
  },
  "logging": {
    "level": "INFO"            // logs go to stdout; journald handles rotation
  }
}
```

- `amp.model` selects a capability profile (see "Model abstraction" below)
  that controls which `0x2F` telemetry fields are treated as valid (e.g. a
  2000S likely populates the PAM2 fields that a 1200S leaves as "Not
  Used") and what the UI labels as the amp's max power/current ratings.
- `tuner.model` is informational for now (status display only, per the
  tuner-command protocol gap) but sets expectations for what status
  fields should be present and will gate active tuner commands later.
- `amp.serial_device` points at a stable path, not a raw `/dev/ttyUSBx`.
  Setup includes a udev rule, e.g.:
  `SUBSYSTEM=="tty", ATTRS{serial}=="<adapter-serial-number>", SYMLINK+="acom-amp"`
  so the symlink survives reboots and other USB-serial devices being
  plugged in.

### Model abstraction

`protocol.py` gets a small per-model capability table (dict or dataclass)
keyed by `amp.model`, e.g.:

```python
AMP_PROFILES = {
    "600S":  AmpProfile(has_pam2=False, max_power_w=600),
    "1200S": AmpProfile(has_pam2=False, max_power_w=1200),
    "2000S": AmpProfile(has_pam2=True,  max_power_w=2000),
}
```

The `0x2F` parser consults the active profile to decide whether to expose
PAM2 temperature/voltage/current fields, and the frontend uses
`max_power_w` for gauge scaling. This is the extension point for adding
real 600S/2000S support later without touching the framing/checksum code.

## Setup Wizard & Antenna Setup

A **Setup** screen (gear icon in the header, available any time, not just
first-run) replaces the old "set it once in config.json and restart" idea
— **config changes are fully live, no service restart.**

- **Amp model** (`600S`/`1200S`/`2000S`) and **tuner/switch**
  (`None`/`04AT`/`06AT`/`20AT`) selects.
  - `GET /api/config` → current `{amp_model, tuner_model}` + connection
    status.
  - `POST /api/config` `{amp_model, tuner_model}` → updates the in-memory
    `AmpProfile`/tuner-presence flag immediately (no serial port
    close/reopen needed — baud/framing are identical regardless of model
    or tuner; only which `0x2F` fields are treated as valid and which UI
    sections show change), and persists the choice to `config.json` so it
    survives a reboot.
- **Antenna Setup** section, shown only when a tuner is selected (04AT/
  06AT/20AT all expose the same 4-antenna-slot tables per `CLAUDE.md`):
  - A 4-row × 11-column matrix: rows = Antenna 1–4, columns = Name (text,
    ≤12 ASCII chars), Type (Sharp/Regular/Wide/Fixed dropdown), then one
    checkbox per band (160/80/60/40/30/20/17/15/12/10/6m).
  - **Pulled from the amp automatically** on serial connect (and via a
    "Refresh from amp" button): `serial_link.py` requests messages `0x16`
    (ATU/ASEL identity — confirms a tuner is actually attached and which
    model), `0x18` (antenna type + band assignments), and `0x1B` (antenna
    names) via the `0x81` Cmd `0x01` "Request specific message" command,
    since those are "generated once on request" rather than streamed.
  - **Saved back to the amp** on "Save": builds and sends `0x85` (set
    antenna names) and `0xA0` (set antenna type + band assignments — same
    16-byte layout as the `0x18` read message, confirmed as the
    computer→amp write counterpart per protocol doc §5.10), then
    re-requests `0x18`/`0x1B` afterward to refresh `AmpState` from the
    amp's own echo rather than trusting the write blindly.
  - `GET /api/antenna/config` / `POST /api/antenna/config` with body
    `{antennas: [{number, name, type, bands: [...]}]}` (4 entries).

`amp_state.py` gains an `antenna_config: list[AntennaSlot]` field
(`number`, `name`, `type`, `band_mask`) populated from `0x18`/`0x1B` on
connect and kept current after any `0x85`/`0xA0` write.

## Backend vs. frontend logging

One systemd service, one `journalctl -u acom-control` stream, but two
distinctly-named loggers so backend and frontend failures stay traceable
to their source instead of interleaving:

- **Backend**: standard Python `logging.getLogger("acom.backend")`
  (further sub-loggers per module are fine, e.g. `acom.backend.serial`,
  `acom.backend.protocol`) — covers serial I/O, framing/checksum errors,
  command queue/retry events, Modbus server activity, and FastAPI request
  errors. Configured once in `app.py` startup from `config.json`'s
  `logging.level`.
- **Frontend**: the browser can't write to journald directly, so
  `app.js` installs a `window.onerror` + `window.onunhandledrejection`
  handler, and wraps the WebSocket/fetch calls' error paths, to `POST
  /api/frontend-log` with `{level, message, stack, url}`. The backend
  route logs that payload through `logging.getLogger("acom.frontend")`
  tagged with the reporting browser's IP — so a dashboard crash/WS drop
  on someone's phone shows up server-side even though no one's watching
  that browser's devtools console.
- **Filtering**: both loggers share the process's stdout → journald, but
  `journalctl -u acom-control` output includes the logger name per line
  (`acom.backend.serial: ...` / `acom.frontend: ...`), so
  `journalctl -u acom-control | grep '^acom.frontend'` isolates
  client-side issues and vice versa for backend-only debugging — no need
  for two separate log files or systemd units.

## UI Design — Themes

Reference material: the real LCD screenshots in `ACOM 1200S User Manual GB
08_2024.pdf` (Figures 3-2, 4-3, 4-4, 5-1–5-7) show a consistent visual
language — dark charcoal background, amber/goldenrod label bars with bold
black text, green digital power readouts, large white frequency readout,
segmented (blocky, not smooth-gradient) bargraphs with a red "do not
enter" zone near max, a status row of amber segments each with a small
colored LED dot, a yellow flashing warning banner, and a bottom soft-key
button bar mirroring the amp's 6 physical buttons.

**Three themes, each with a Light and Dark mode (6 combinations total):**

1. **ACOM** — pixel-faithful recreation of the real LCD above. ACOM Dark
   is effectively a direct skin of the manual screenshots; ACOM Light is
   the same layout with a daylight panel treatment.
2. **Classic** — a 1950s tube-amplifier aesthetic: round analog
   needle-meter gauges on a printed arc scale, glass-dome pilot lamps,
   warm Bakelite/cream or brushed-aluminum panel, chunky labeled buttons,
   slab-serif/engraved-panel typography. This is a genuinely different
   era than the real ACOM LCD — bargraph/LED displays didn't exist in the
   1950s, so this theme earns its own gauge widget rather than reusing
   the bargraph.
3. **Modern** — contemporary flat dashboard: smooth gradient bargraph
   fill, rounded corners, flatter status chips, clean sans-serif type.

Decisions:
- **ACOM and Modern share one component set** (bargraph, status row,
  button bar) — they differ only via CSS custom properties (colors,
  corner radius, bargraph fill style, fonts), no markup divergence.
- **Classic gets its own gauge widget**: a round needle-meter component
  (SVG arc + rotating needle) replaces the bargraph for forward power, PA
  temperature, reflected power, input power, and PA DC current. The
  status row's small LED dot is still shared across all three themes —
  styled as a glowing "pilot lamp" for Classic via CSS only (no new
  component needed there), same for the button bar (vintage chunky style
  achievable with borders/bevel/shadow, still CSS-only).
- **Scope v1**: only the live "Basic Screen" dashboard gets the full
  themed look-alike treatment, in whichever theme is active. Settings
  screens (CAT settings, user preferences, faults log, restore defaults)
  are plain themed web forms/tables, not pixel recreations of each LCD
  menu.
- **Default on first visit: ACOM Dark** — closest to the original "look
  like the amp's display" ask.

### Component architecture

Because Classic needs a different widget for the main telemetry readouts,
the frontend needs a small render-time abstraction rather than pure CSS
theming:

```
frontend/
  styles/
    base.css            structural layout only (grid, spacing) — theme-agnostic
    components.css       status row, readouts, button bar — var()-driven,
                         shared by all three themes
    gauges/
      bargraph.css        segmented (ACOM) vs smooth-gradient (Modern) fill,
                         both driven by the same bargraph markup
      needle-meter.css     arc scale, needle, pilot-lamp glow (Classic)
    themes/
      acom.css            [data-theme="acom"][data-mode="dark|light"]
      classic.css          [data-theme="classic"][data-mode="dark|light"]
      modern.css           [data-theme="modern"][data-mode="dark|light"]
  gauges.js              renderBargraphGauge(el, {value,min,max,label,unit})
                         renderNeedleGauge(el, {value,min,max,label,unit})
                         a GAUGE_WIDGET_BY_THEME lookup ({acom:'bargraph',
                         classic:'needle', modern:'bargraph'}) picks which
                         render fn mounts into each gauge slot; switching
                         themes re-mounts the gauges, data updates just
                         update values in whichever widget is mounted
  app.js
  theme.js               reads/writes {style, mode} to localStorage, sets
                         data-theme/data-mode, triggers gauges.js to
                         re-mount on style change; a tiny inline <script>
                         in <head> applies the saved theme before first
                         paint to avoid a flash of the wrong one
```

The needle-meter widget: an SVG arc with tick marks, a `<line>`/`<path>`
needle whose rotation is computed from `(value - min) / (max - min)`
mapped to the gauge's sweep angle (e.g. -45° to +45°), updated with a CSS
`transition: transform` for smooth movement rather than per-frame JS
animation — no extra libraries needed.

Indicative palette (ACOM Dark pulled directly from the manual
screenshots; others derived to keep each theme internally consistent):

| Variable | ACOM Dark | ACOM Light | Classic Dark (tube glow) | Classic Light (daylight Bakelite) | Modern Dark | Modern Light |
|---|---|---|---|---|---|---|
| `--bg-page` | `#1c1c1c` | `#e8e4da` | `#241a12` (warm near-black) | `#e9ddc4` (cream Bakelite) | `#121417` | `#f5f6f8` |
| `--bg-panel` | `#2b2b2b` | `#f2efe6` | `#3a2a1d` (dark walnut/brown) | `#f3e9d2` | `#1e2126` | `#ffffff` |
| `--accent-label` | `#e0a030` (amber bar) | `#d99a2b` | `#c97f2c` (warm brass) | `#8a5a2b` (engraved brass) | `#4fd1c5` (teal) | `#2a9d94` |
| `--text-primary` | `#f2f2f2` | `#222222` | `#f0dcc0` (warm ivory) | `#2a1d12` | `#e8e8ea` | `#1a1a1a` |
| `--readout-power` | `#7ac142` (green) | `#2e7d32` | n/a (needle position, not digital) | n/a | `#4fd1c5` | `#1f8a7a` |
| `--readout-freq` | `#ffffff` | `#1a1a1a` | `#ff9a3c` (nixie-tube orange glow) | `#2a1d12` (engraved numerals) | `#ffffff` | `#1a1a1a` |
| gauge style | segmented bargraph | segmented bargraph | round needle meter | round needle meter | smooth gradient bargraph | smooth gradient bargraph |
| pilot lamp / LED | amber segment + LED dot | amber segment + LED dot | glowing glass-dome lamp | glass-dome lamp (unlit look) | flat status chip | flat status chip |
| `--warn-bg` | `#f5e050` | `#f5e050` | `#e2b23c` (amber warning lamp) | `#e2b23c` | `#f0b429` | `#f0b429` |
| `--radius` | `0` | `0` | `6px` (rounded meter bezel) | `6px` | `10px` | `10px` |
| `--font-family` | monospace-leaning | monospace-leaning | slab-serif / engraved-panel style | slab-serif / engraved-panel style | system sans | system sans |

Exact hex values and the needle-meter's visual detail get fine-tuned
against real references while building — this table is the starting
point, not final.

### Theme switcher

A small control in the dashboard header: a 3-way style selector
(ACOM / Classic / Modern) plus a light/dark toggle (sun/moon icon),
independent of each other. Choice persists to `localStorage` and is
restored by `theme.js` on load.

## Architecture

```
┌─────────────┐   USB-RS232    ┌────────────────────────────────────┐   WebSocket/HTTP   ┌────────────┐
│ ACOM amp     │◄──────────────►│ Raspberry Pi (Python/FastAPI)       │◄───────────────────►│  Browser   │
│ (DB9 RS-232) │  9600 8N1      │  serial_link / protocol / amp_state │   on local network   │  dashboard │
└─────────────┘                 │  FastAPI app  ──┬── Modbus TCP server │◄──────────────┐    └────────────┘
                                 └─────────────────┼──────────────────┘                 │
                                                    │                      Modbus TCP    │
                                                    └──────────────────────────────────► 3rd-party app
                                                                                            (e.g. QSO Director)
```

Single always-running asyncio process on the Pi:
1. A background task owns the serial port, continuously reads bytes,
   frames them per the protocol, verifies checksums, and dispatches
   decoded messages.
2. Decoded status messages update a single in-memory `AmpState` object and
   are broadcast to any connected WebSocket clients.
3. Outgoing commands (from REST handlers *and* the Modbus server) are
   placed on the same queue that the serial task drains, respecting the
   ~10 ms inter-message pacing and the ACK/retry rules (resend up to 5x
   if an amp-initiated message isn't ACKed, per the protocol doc). A
   single shared queue keeps commands from the web UI and from 3rd-party
   apps from racing each other on the wire.
4. FastAPI serves both the API/WebSocket and the static dashboard files —
   no separate frontend build/server needed.
5. A Modbus TCP server (via `pymodbus`) runs alongside FastAPI in the same
   process, reading from the same `AmpState` and writing to the same
   command queue, so the web UI and any Modbus client stay in sync.

## Directory layout (new, under this folder)

```
Acom Remote/
  CLAUDE.md                      (existing)
  PI_CONTROL_PLAN.md             (this file)
  acom-control/
    config.json                  amp/tuner model, serial device, web + modbus settings
    backend/
      app.py                     FastAPI app: routes, websocket, startup/shutdown
      serial_link.py             pyserial I/O, framing, checksum, ACK/retry, pacing
      protocol.py                message encode/decode: 0x81 command builder,
                                  0x2F telemetry parser, 0x16/0x18/0xA0 ATU &
                                  antenna status parsers, 0x21 fault-bit decoder,
                                  0x31-0x34 hard-fault record parser
      amp_profiles.py             per-model (600S/1200S/2000S) capability table
      amp_state.py                AmpState dataclass + thread/async-safe store
      modbus_server.py            pymodbus TCP server mapping registers/coils to
                                  AmpState + the shared command queue
      config.py                  loads/validates config.json
      requirements.txt
    frontend/
      index.html                 single-page dashboard (vanilla JS, no build step)
      app.js                     WebSocket client, REST calls, UI update logic
      gauges.js                  bargraph vs needle-meter widget rendering,
                                  theme-to-widget lookup, re-mount on theme switch
      theme.js                   theme/mode selection + localStorage persistence
      styles/
        base.css                 structural layout (theme-agnostic)
        components.css            status-row/readouts/button-bar, all var()-driven
        gauges/
          bargraph.css             segmented (ACOM) vs smooth-gradient (Modern)
          needle-meter.css          arc scale, needle, pilot-lamp glow (Classic)
        themes/
          acom.css                 ACOM Dark + ACOM Light variable sets
          classic.css               Classic Dark + Classic Light variable sets
          modern.css                Modern Dark + Modern Light variable sets
    setup/
      99-acom-amp.rules          udev rule template for the stable serial symlink
      acom-control.service       systemd unit template
    README.md                    setup + run instructions
```

Keeping the frontend as plain HTML/JS/CSS avoids needing Node/npm on the Pi
and matches the project's scale (a personal local-network control panel).

## Implementation steps

0. **`config.py` / `config.json` / `amp_profiles.py` — config & model layer**
   - `config.py` loads and validates `config.json` (device path, amp/tuner
     model, web + modbus settings).
   - `amp_profiles.py` defines the `AMP_PROFILES` table described above;
     everything else (protocol parsing, UI scaling) reads from the active
     profile instead of hardcoding 1200S assumptions.
   - Fail fast and loudly at startup if `amp.model` isn't a known profile
     or `amp.serial_device` doesn't exist — don't silently fall back.

1. **`protocol.py` — message layer**
   - Implement `checksum(bytes) -> int` per `256 - (sum(frame[:-1]) & 0xFF)`.
   - `build_command(cmd, param_hw=0, param_lw=0, buttons=0) -> bytes` for the
     `0x81` envelope (covers power/mode change `0x02`, band/antenna cycle
     `0x09`, buzzer `0x0A`, ATU management `0x0E`, etc. per `CLAUDE.md`).
   - `parse_frame(bytes) -> Message | None`: validates start byte/length/
     checksum, returns a tagged object by address.
   - Per-address parsers at minimum for: `0x2F` (main telemetry — the
     dashboard's primary data source), `0x16` (ATU/ASEL identity), `0x18`
     (antenna assignments, get), `0x1B` (antenna names, get), `0x27`
     (current antenna/band/segment status), `0x21` (fault/warning
     bitfields), `0x86` ack. Also `build_command` needs an `0x85` builder
     (set antenna names — same 52-byte layout as `0x1B`, computer → amp)
     and an `0xA0` builder (set antenna assignments/band matrix — same
     16-byte layout as `0x18`, computer → amp; see `CLAUDE.md`). Other
     addresses from the doc can be stubbed/logged initially and filled in
     as needed.
   - Re-verify exact field offsets/addresses directly against the PDF
     while coding this file — `CLAUDE.md` is a summary, the PDF is ground
     truth.

2. **`serial_link.py` — transport layer**
   - Open the USB-serial device at 9600 8N1 via `pyserial`.
   - **Startup handshake** (do this before trusting anything else): send
     the command to force **HEX (binary) message format** (§5.6 — the
     amp can be left in ASCII mode from a prior terminal session, which
     would silently break all framing/checksum parsing), then send the
     **enable automatic telemetry** command (§5.7/§5.8) so `0x2E`/`0x2F`
     start streaming without assuming they're on by default.
   - Async read loop: byte-level scanner for `0x55` start, read length
     byte, read remaining frame bytes, pass complete frames to
     `protocol.parse_frame`.
   - On receipt of an amp-initiated message requiring confirmation, send
     the `0x86` ack frame back.
   - Outbound command queue with the required ~10 ms spacing; retry logic
     (max 5 attempts) if an expected ack doesn't arrive.
   - Expose an `asyncio.Queue`/callback for decoded messages so `app.py`
     can update state and fan out to websockets without the transport
     layer knowing about HTTP/WS.

3. **`amp_state.py` — shared state**
   - Dataclass mirroring the `0x2F` fields (mode, flags, power/SWR,
     voltages, temps, fault code) plus latest ATU/ASEL identity and
     antenna assignment table.
   - `update_from(message)` mutation + a way to serialize to JSON for both
     the WebSocket push and a `GET /api/status` snapshot endpoint.

4. **`app.py` — FastAPI app**
   - `GET /api/status` — current `AmpState` snapshot (useful for initial
     page load before the websocket connects).
   - `WS /ws/status` — pushes `AmpState` JSON on every update.
   - `POST /api/power` `{on: bool}` → mode-change command (`0x02`/`0x0A` or
     `0x07`/OPR per the doc).
   - `POST /api/mode` `{mode: "stb"|"opr_rx"|"opr_tx"|...}`.
   - `POST /api/band` `{direction: "next"|"prev"}`, `POST /api/antenna`
     `{action: "next"}` (cycle-only for normal tuned operation — direct
     select exists only as the ATU bypass command, which skips tuning;
     document this limitation in the UI).
   - `GET /api/config` / `POST /api/config` `{amp_model, tuner_model}` →
     live profile swap + persists to `config.json` (see "Setup Wizard &
     Antenna Setup" above — no restart needed).
   - `GET /api/antenna/config` / `POST /api/antenna/config`
     `{antennas: [{number, name, type, bands: [...]}]}` → the Antenna
     Setup matrix; `POST` builds and sends `0x85` (names) + `0xA0`
     (type/band assignments), then re-requests `0x18`/`0x1B` to refresh
     `AmpState` from the amp's own echo.
   - `POST /api/buzzer` volume/tone.
   - `POST /api/frontend-log` `{level, message, stack, url}` → logged
     server-side via `logging.getLogger("acom.frontend")` (see "Backend
     vs. frontend logging" above); no response body needed beyond 204.
   - **OPR/TX requires confirmation**: `POST /api/mode {mode: "opr_tx"}`
     is rejected unless the request also carries a short-lived "armed"
     token obtained from a separate `POST /api/mode/arm_tx` call; the web
     UI's confirm dialog calls `arm_tx` then the mode change. This keeps
     a single stray mode-change call (UI misclick or buggy client) from
     keying the amp.
   - **Destructive commands are web-UI-only**: erase tuning memory,
     factory reset, and fault-log clear (`0x0D`, parts of `0x10`) live
     behind their own confirm-gated endpoints (e.g. `POST
     /api/atu/erase`) and are never exposed via `modbus_server.py`.
   - Mount `frontend/` as static files at `/`.
   - Startup hook opens the serial link and starts the background task;
     shutdown hook closes it cleanly.

5. **`modbus_server.py` — 3rd-party control API**
   - Use `pymodbus`'s async TCP server, started alongside FastAPI in the
     same event loop (startup/shutdown hooks mirror `app.py`'s).
   - Draft register/coil map (finalize once QSO Director's expected
     addressing is confirmed — Modbus itself doesn't standardize *what*
     lives at which address, only the transport):
     - Holding registers (read): forward power, reflected power, SWR×100,
       HV1 (×10V), carrier frequency (kHz), amp mode, active fault code —
       i.e. the same fields already in `AmpState` from message `0x2F`.
     - Coils (read/write): power on/off, OPR/STB mode.
     - A couple of write-only registers/coils for "next band" / "next
       antenna" pulses, mirroring the `0x09` command.
   - **Scope deliberately limited to normal operating commands.** Erase/
     factory-reset/fault-clear commands and raw OPR/TX access (see the
     arm/confirm flow in `app.py` above) are NOT exposed here — a Modbus
     "key the amp" coil only works if the amp is already armed via the
     web UI first, or (simpler alternative, decide while implementing)
     Modbus is restricted to telemetry reads + band/antenna cycling only,
     with mode changes staying web-UI-only.
   - All writes funnel into the same command queue `serial_link.py`
     drains — the Modbus server never talks to the serial port directly.
   - No auth on the Modbus port either, per the v1 decision — document
     clearly in the README that this means anything on the LAN can key
     the amplifier, and firewall/VLAN it yourself if that's a concern.

6. **Frontend dashboard**
   - On load: `fetch('/api/status')` for initial state, then open
     `new WebSocket(...)` for live updates.
   - Layout mirrors the amp's own Basic Screen (see "UI Design — Themes"
     above): forward power gauge, PA temp + reflected power row, input
     power + PA DC current row, large frequency readout, status row
     (mode/RX-TX/CAT/remote-control indicators), warning banner, and a
     bottom button bar — themed via the ACOM/Classic/Modern ×
     Light/Dark system (`theme.js` + `gauges.js`), with Classic swapping
     in the needle-meter widget instead of the bargraph for the five
     telemetry gauges.
   - Controls: power on/off (TX gated behind the arm/confirm flow), mode
     buttons, band next/prev, antenna next, buzzer test. Each simply
     `fetch(..., {method:'POST', ...})`.
   - **Setup screen** (gear icon): amp/tuner model selects bound to
     `GET`/`POST /api/config`, and the Antenna Setup name/type/band
     matrix bound to `GET`/`POST /api/antenna/config`, shown only when a
     tuner is selected.
   - **Error reporting**: `app.js` installs `window.onerror` /
     `window.onunhandledrejection` handlers and wraps WebSocket/fetch
     error paths to `POST /api/frontend-log` (see "Backend vs. frontend
     logging" above) — fire-and-forget, never blocks the UI on a failed
     log post.
   - Keep it a single page, no framework/build step, consistent with the
     "small personal tool" scale of this project.

7. **Deployment**
   - `requirements.txt`: `fastapi`, `uvicorn[standard]`, `pyserial`,
     `pymodbus`.
   - `setup/99-acom-amp.rules`: udev rule template keyed to the USB-serial
     adapter's serial number, installed to `/etc/udev/rules.d/`, so
     `config.json`'s `amp.serial_device` can point at a stable
     `/dev/acom-amp` symlink.
   - `setup/acom-control.service`: systemd unit running under a
     dedicated unprivileged user (member of the `dialout` group),
     `StandardOutput=journal` so logs flow to `journald`.
   - `README.md` documents: Raspberry Pi OS Lite (64-bit) install, the
     apt/pip packages above, USB-serial adapter wiring to the amp's DB9
     pins (per `CLAUDE.md`'s Table 2-3 pinout — note the DSR/CTS "Remote
     Power On" ambiguity to verify empirically), finding the adapter's USB
     serial number (`udevadm info`) to fill in the udev rule, setting a
     DHCP reservation for the Pi, running via `uvicorn backend.app:app
     --host 0.0.0.0 --port 8000` during development, and installing
     `setup/acom-control.service` so it survives reboots.

## Verification

- **Bench test without the amp first**: a small loopback/mock serial
  source (or a second USB-serial pair) to confirm framing/checksum code
  round-trips correctly before touching real hardware.
- **Connect to the amp powered OFF first**, confirm the Pi can open the
  port with no errors, then power the amp to STBY and confirm `0x2F`
  telemetry frames start arriving and parse without checksum errors.
- **Dashboard smoke test**: load the page on another device on the LAN,
  confirm live telemetry updates over the WebSocket, and that each control
  button produces the expected `0x81` command bytes (verify via a serial
  sniffer/logging in `serial_link.py` before trusting it against the real
  amp).
- Exercise power on/off, mode changes, and band/antenna cycling one at a
  time, watching the amp's own front panel to confirm each command does
  what's expected before chaining UI actions together.

## Other considerations raised but not yet decided

- **ACOM eBox conflict:** the amp has one RS-232 port. If you ever want
  to fall back to a real ACOM eBox, it and the Pi can't both be plugged
  in at once — purely a physical/README note, not a design problem. The
  amp's separate CAT port (transceiver frequency-following) is unrelated
  and unaffected either way.
- **Resilience:** the serial task should detect a dropped/unplugged USB
  adapter and retry opening the device with backoff, and `AmpState`
  should expose a `comms_ok` flag so the UI can show "lost link to amp"
  instead of silently freezing on stale data.
- **Multi-client command visibility:** with the web UI, the Modbus API,
  and the amp's own front panel all able to issue commands, the dashboard
  should surface the protocol's own `FLAG_Last_Com` bit (local vs.
  remote) so it's clear who last touched the amp.
- **Firmware/bootloader commands (`0x90`-`0x93`):** deliberately excluded
  from this app for safety; firmware updates stay a manual ACOM Terminal S
  process.
- **Telemetry history & fault log viewer:** not in v1, but `amp_state.py`
  is the natural place to add either a simple SQLite ring buffer (for
  SWR/temp trend charts) or a decoder for the Hard Fault event records
  (`0x31`-`0x34`) once the live dashboard is working.
- **mDNS hostname** (e.g. `acom.local` via `avahi-daemon`) so the
  dashboard is reachable from a phone/tablet in the shack without
  memorizing an IP.
- **Mobile-responsive layout** for the frontend, since it'll likely get
  used from a phone as often as a desktop browser.
- **Log rotation**: telemetry/debug logging on a Pi running 24/7 should
  rotate (size or time based) so it doesn't fill the SD card over months
  of uptime — plain `logging.handlers.RotatingFileHandler` is enough for
  v1, no need for anything fancier.

## Follow-ups (explicitly out of scope for v1)

- Active 06AT/20AT tuner commands, pending protocol confirmation from
  ACOM/dealer per the open question already logged in `CLAUDE.md`.
- Direct "select antenna N for normal tuned operation" (not bypass) if/when
  a non-cycling command is found.
- Finalizing the exact Modbus register/coil addressing once QSO
  Director's (or whichever app's) expected map is confirmed.
- Telemetry history storage/graphing and the hard-fault event log viewer
  (see above) — straightforward additions once v1's plumbing exists.
- Optional auth (shared token) for the web UI/API/Modbus server, if the
  "trust the LAN" v1 decision ever needs revisiting.

## Status

The initial `acom-control/` build now exists (config, backend, frontend,
setup/ files per the Directory layout section above). It has **not** been
tested against real hardware - there's no Pi or amp attached to the
machine it was written on. Specifically still needing real-hardware
verification before this is trusted:

- The ACK/retry timing model in `serial_link.py` (the doc under-specifies
  how outbound-command acknowledgement is supposed to work beyond the
  amp-initiated-message case).
- The startup handshake's forced-HEX-mode step (`0x90`, which the doc
  itself marks "Obsolete! (Removed)") - sent as a best-effort no-op.
- `modbus_server.py`'s pymodbus API usage - written against the 3.x async
  API from memory/docs, not verified against the actual installed version
  (pymodbus isn't installed on the authoring machine).
- `0x27`'s `segment_status_raw` bit-level meaning (7 raw bytes exposed,
  not decoded - the doc doesn't fully clarify the per-segment bit layout).
- `protocol.FAULT_CODE_NAMES` only covers a representative subset of the
  ~90-entry fault table in the doc's section 3.7.1; everything else still
  surfaces as a raw hex code to the frontend.
- The three frontend themes are a first pass (correct structure/data
  binding, not pixel-tuned against the real LCD screenshots yet).

Framing/checksum, the 0x81 command builders, and all the telemetry/antenna
message parsers were round-tripped with synthetic frames (not real serial
data) and compile cleanly - see the fork's implementation report for what
was exercised.

### Fix pass: code-review findings + protocol-coverage gaps

A `/code-review` pass plus a manual protocol-coverage audit (every message
address and `0x81` sub-command in the doc checked against the
implementation) turned up real gaps, all now addressed:

- **Command confirmation**: `0x01`/`0x03`/`0x04` (malformed-reply/OK/ERR)
  are now parsed and surfaced to `AmpState.last_command_result`.
  `serial_link.py` gained `send_command()` - writes a frame, waits for the
  amp's OK/ERR, retries up to 5x on timeout (doc section 1's resend rule),
  and reports success/failure to the caller. All command-sending routes in
  `app.py` (power/mode/band/antenna/buzzer/antenna-config writes/factory
  reset/ATU erase) go through it and return HTTP 502 on a confirmed
  failure; status-query sends (`build_request_message` and the new ATU
  tuning-record request) stay fire-and-forget, since their "reply" is the
  requested message itself.
- **`0x2E`** (ATU Measure menu telemetry, sent instead of `0x2F` while the
  amp's own front panel shows that menu), **`0x1A`** (per-band/segment ATU
  tuning quality) and **`0x2A`** (ATU Service menu: working hours, tune%,
  serial) are now parsed and in `AmpState`; `0x2A`'s fields surface in a
  read-only "ATU Status" panel in the Antenna Setup screen.
- **ATU memory erase is now real.** The old `/api/atu/erase` route only
  ever implemented Factory Reset (`0x81` Cmd `0x0D`) - renamed to
  `/api/factory-reset`. A genuine `POST /api/atu/erase-memory` (Cmd `0x10`:
  erase current/specific/all/whole-memory) and `POST
  /api/atu/delete-segment-tuning` (Cmd `0x12`) were added, both confirm-
  gated server-side. Per explicit user instruction, both live inside the
  Antenna Setup screen behind a collapsed "Advanced / Danger Zone"
  `<details>` section with a native `confirm()` double-check before
  firing - not reachable by a stray click anywhere else.
- **`0x81` Cmd `0x0F`** (request a specific ATU tuning record) was added as
  `POST /api/atu/tuning-record`, used by the Danger Zone's segment-delete
  tool to look up what's there before deleting it.
- **`try_parse_frame`** now rejects a length byte over the doc's 72-byte
  max instead of stalling the parser waiting for an implausible frame.
- **TX-arm hardening**: `/api/mode/arm_tx` now returns a single-use random
  token that `/api/mode {mode: "opr_tx"}` must present, instead of a bare
  "any client within 15s" window - closes the gap where another client/
  script on the LAN could ride someone else's confirm dialog.
- **Modbus coil-pulse coalescing fixed**: coil edge-detection now polls at
  100ms (was 500ms, shared with the telemetry mirror), so two quick
  repeated pulses from a 3rd-party client aren't collapsed into one.
- **Frontend**: `apiFetch()` now throws on non-2xx instead of only
  logging; every button handler is wrapped to catch and show a visible
  error banner instead of failing silently (this is what makes the TX-arm
  fix actually visible to the operator - a failed arm/key no longer closes
  the confirm dialog as if it succeeded). The hot WebSocket-message render
  path no longer re-reads `localStorage` on every tick - theme is cached
  and only refreshed on the `acom-theme-change` event. `app.py`'s
  fire-and-forget WebSocket-broadcast tasks are now tracked (no more
  GC-eligible, exception-swallowing orphan tasks), and `POST /api/config`
  has partial-update semantics (omitting `tuner_model` no longer silently
  clears it).
- One code-review finding was investigated and determined to be a false
  positive (not fixed, deliberately): a claim that `serial_link.py` should
  ACK a received `0x86` frame - `0x86` is documented exclusively as a
  computer→amplifier message (doc section 5), so the amp never legitimately
  sends one to us; the finding was citing a stale/incorrect description
  that didn't match this file's current content.

Still unverified against real hardware (expected - no Pi/amp attached to
the authoring machine): the exact `send_command()` timeout/retry timing,
whether `0x2A` is actually requestable via the generic Cmd `0x01`
"request specific message" mechanism (added to the startup pull on that
assumption), and everything else already listed above this subsection.
