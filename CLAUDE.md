# ACOM Remote — Raspberry Pi Web Control Project

## Goal

Build a Raspberry Pi–based remote control for an **ACOM 1200S** HF amplifier
(and eventually its **ACOM 06AT/20AT** antenna tuner/switch). The Pi talks to
the amp over its rear-panel RS-232 port and runs a backend service that hosts
a web page on the local network, giving full front-end control (status
display + commands) from a browser — essentially a DIY alternative to ACOM's
own eBox Ethernet Remote Control device.

Note: ACOM's own answer to this is the **eBox** (Ethernet Remote Control,
sold as an accessory) — it almost certainly just bridges this same RS-232
protocol to Ethernet. Worth keeping in mind as a reference point / fallback
if reverse-engineering gaps become blockers.

## Source documents (in this folder)

- `ACOM A600S A1200S Serial Protocol V1.3.pdf` — the only document with the
  actual byte-level serial protocol. Covers amplifiers **A600S / A1200S**
  (dated 2018, author Nikolay Nenov). Defines ATU type `04AT` and antenna
  selector type `10AS` only.
- `ACOM 1200S User Manual GB 08_2024.pdf` — user manual for the **1200S**
  (note: "1200S", not "A1200S" — possibly a later/renamed model vs. the
  protocol doc above; unconfirmed whether the wire protocol is identical).
  Its RS-232 remote-control section (6.2, p.54) explicitly says *"For ACOM
  1200S RS-232 interface protocol, please, contact your dealer"* — no raw
  commands are included here, only the connector pinout.
- `ACOM 06AT_20AT User Manual GB 09_2025.pdf` — user manual for the current
  tuner/switch models. No raw serial commands either — just procedures for
  using the "ACOM Terminal S" GUI tool for memory dump/load and firmware
  update (via the amplifier's RS-232 port, 9600 8N1).

**Open gap:** the 06AT/20AT are newer models than what the only protocol doc
covers (04AT tuner / 10AS selector). It's unconfirmed whether the 06AT/20AT
speak the same amp↔ATU sub-protocol or something extended. Full tuner/switch
command discovery is still in progress — see "Open questions" below.

## Protocol facts confirmed from the 2018 serial protocol PDF

- **Transport:** RS-232, rear panel. **9600 baud, 8N1.** ~10 ms pause
  required between messages. Max message length 72 bytes.
- **Framing:** `[0x55 Start][Address][Length][...Data...][CHKSUM]`
  - `Length` = total byte count including start byte and checksum.
  - `CHKSUM = 256 − (SUM(Byte[0..N-1]) & 0xFF)`; a correctly received
    message sums to 0 mod 256 across all bytes including checksum.
- **Handshake:** any message initiated by the amp must be ACKed by the
  computer with message `0x86`, or the amp resends it (up to 5 attempts,
  then discards). Likewise unconfirmed messages from the computer are
  resent/discarded the same way.
- **RS-232 pinout (1200S manual, Table 2-3, DB9F):** pin 2 TxD, pin 3 RxD,
  pin 5 GND, pin 6 DSR ("Remote Power On" input), pin 8 CTS ("Remote Power
  On" input). Pins 1/4/7/9 not connected. Note both DSR *and* CTS are
  labeled "Remote Power On" — worth verifying in practice which one(s)
  actually matter for powering the amp on remotely from the Pi.

## Command messages (computer → amplifier), address `0x81`

Envelope: `[0x55][0x81][Len=8][Cmd][Param HW][Param LW][Buttons][CHKSUM]`

Known `Cmd` (Byte 3) values:
- `0x00` Empty command
- `0x01` Request specific message (Byte4/5 = message number)
- `0x02` Amplifier mode change (Byte5): `0x01/0x02` restart, `0x03` Debug,
  `0x04` Service, `0x05` STB, `0x06` OPR/RX, `0x07` OPR/TX, `0x09` "Set
  System Parameters" menu, `0x0A` turn OFF, `0x40` prohibit OPR/TX,
  `0x80` allow OPR/TX
- `0x03` Request Hard Fault event record (Byte5 = record number)
- `0x04` Update User Flags (bitfield: Operate Access, ON_BTN pulse mode,
  ATAC manual/auto, buzzer on/off, temp unit C/F, Auto Operate, Extra
  Cooling, etc.)
- `0x05`–`0x07` CAT interface settings (type/command-set/baud, byte
  spacing, polling time)
- `0x08` Clear SOFT FAULT errors / last-power-off-was-hard-fault flag
- `0x09` **Manual antenna/band change** (Byte5): `0x80` Next Band,
  `0x40` Previous Band, `0x20` Next Segment, `0x10` Previous Segment,
  `0x30` **Next Antenna** — *cycling only, no direct "select antenna N"
  found yet*
- `0x0A` Buzzer volume/tone selection
- `0x0B` Dump entire LOG file over RS-232
- `0x0C` Service Mode test commands (idle current, HV, relay tests, band
  relay tests, fan speed tests)
- `0x0D` Factory reset options (user prefs, hour counter, error log,
  all, antenna assignments)
- `0x0E` **ATU Management** (Byte5): `0x01` Full Tune, `0x02` **Bypass
  Command (Number of Antenna)**, `0x2E`/`0x2F` activate cyclic display
  messages, `0xFF` assign/unassign ATU. **These four are the entire
  command set** (confirmed directly from the doc's own §5.1 table, re-read
  on request) - there is no documented "resume normal tuned operation
  without a fresh tuning cycle" command. But the amp's own front panel
  (hold TUNE 1s, a toggle) leaves Bypass by *recalling* stored tuning
  (logged 2026-10-23: Recalling -> Memory Settings, no Matching Cycle),
  so `/api/atu/bypass` now re-sends Bypass (`0x02`) as the remote
  equivalent of that toggle, instead of Full Tune, and reports
  `still_bypassed` if the amp ignores it. **Confirmed working on real
  hardware** (Tyler, 2026-10-23). `/api/atu/tune` exposes Full Tune
  standalone (and is the fallback for leaving Bypass). After a Full Tune
  with no RF, the amp sits in `0x27` label 3 "Matching Cycle" and
  re-sends that same `0x27` about once a second until it gets a carrier;
  Bypass clicks in that state don't change anything (the dashboard's
  button reads "Tuning…" then).
- Buzzer (`0x0A`), like the band/antenna/mode commands, is never ACKed
  by the amp - it goes through `_send_pulse_command()` (one send, no
  retry); the retrying path beeped up to 5 times and returned 502.
- `0x0F` Request specific ATU tuning record (by antenna/band lookup index)
- `0x10` ATU Service commands: erase current/specific/all tunings, erase
  whole ATU memory, memory dump-to-file / load-from-file, exit service menu
- `0x11` Set "Tuning Outdated" time threshold [h]
- `0x12` Delete tuning of a specific segment

## Antenna naming/switching (confirmed from the 2018 serial protocol PDF, §3.6.2/5.4/5.10)

The amp's own antenna subsystem (fed by ATU type `04AT` / ASEL type `10AS`)
only exposes **4 named/typed antenna slots** over RS-232, even though the
antenna *numbering* used elsewhere in the protocol goes up to 10 (matching
the "10AS" model name) — see the discrepancy noted below.

- **Antenna names (get):** address `0x1B`, amp → computer, 52 bytes. Bytes
  3–50 = 4 antenna names × 12 ASCII chars each (Antenna 1 = bytes 3–14,
  Antenna 2 = 15–26, Antenna 3 = 27–38, Antenna 4 = 39–50).
- **Antenna names (set):** address `0x85`, computer → amp, same 52-byte
  layout as `0x1B`. This is the actual "name your antennas" command.
- **Antenna assignments (get):** address `0x18`, amp → computer, 16 bytes.
  Byte 3–6 = antenna type per slot (0 Sharp / 1 Regular / 2 Wide /
  3 Fixed), bytes 7–14 = per-antenna band-low/band-high bitmasks
  (bit 0=160m … bit 10=6m) for antennas 0–3.
- **Antenna assignments (set):** address `0xA0`, **computer → amplifier**
  (confirmed — `0xA0` falls under protocol doc §5, "Messages and commands
  from the computer to the main CPU of the amplifier," not under the
  amp-status sections). Identical 16-byte layout to `0x18` ("Similar to
  Message 0x18" per the doc), just the opposite direction — this is the
  actual "write the band↔antenna matrix" command.
- **Band bytes are low byte first (confirmed 2026-10-27).** A real 1200S
  sent `0x18` = `01 01 01 01 ff 07 00 00 00 00 ff 07`: `ff 07` read
  low-first is `0x7FF`, exactly the 11 bands. `protocol.py` used to read
  (and write) them high-first, which garbled Antenna Setup's band list.
- **Writes are confirmed by a read-back, not OK (confirmed 2026-10-27 for
  names).** A `0x85` names write is never answered with `0x03`; the amp
  sends back `0x1B` with the names it stored, ~150ms later. So
  `send_command(..., echo_address=0x1B)` counts that as confirmation.
  Before this, Save sent the names 5 times, returned 502, and never sent
  the `0xA0` assignments. `0xA0` is assumed to answer with `0x18` the same
  way (an OK also counts) - NEEDS REAL-HARDWARE VERIFICATION.
- **Reading them back: requests must go one at a time (2026-10-27).** The
  handshake used to fire its six "request specific message" frames 10ms
  apart; across all logs the amp answered `0x12` 18/27 times, `0x16`/`0x18`
  2/27, `0x1B` and `0x11` never (once a `0x01` "malformed" came back).
  `serial_link._pull_config()` now sends each request, waits up to 0.5s
  for that message, retries 3x, then moves on; `/api/resync` waits for
  it. NEEDS REAL-HARDWARE VERIFICATION that `0x1B` then answers at all.
- **Antenna switching (cycle only):** `0x81` Cmd `0x09` Byte5=`0x30`
  "Next Antenna" — confirmed there is no "Previous Antenna" counterpart,
  only Next Band/Previous Band/Next Segment/Previous Segment/Next Antenna.
- **Closest thing to direct antenna select:** `0x81` Cmd `0x0E` (ATU
  Management) Byte5=`0x02` "Bypass Command", where **Byte4 = Number of
  Antenna [1..10]** — this bypasses the ATU and switches straight to that
  antenna number. It's a bypass/no-tune action, not a "select antenna N for
  normal tuned operation" command — still unconfirmed whether anything else
  does that directly.
- **Current antenna status:** address `0x27` (amp → computer, generated only
  on band/segment change), Byte3 = "Number of the selected antenna [1..10]",
  Byte4 = type (+ "is default" flag in MSB), plus current band/segment info
  and an `ATU_Label` status enum (Init/Recalling/Full Tune/Mem Transfer/
  Completed/Bypass/Matched/Coarse Match/No Match/Outdated/Memory Settings).

**Antenna numbers are 0-based on the wire (confirmed 2026-10-23, contradicts
the doc).** The doc says `[1..10]` for the `0x27` antenna byte, but a real
1200S sent `03` while its front panel showed `A4R` (type byte `01` =
Regular matched the "R"), and an amp snapshot captured a raw `0` - impossible
under a 1-based range. The doc's own `0x18`/`0xA0` tables already label the
slots `Ant0`-`Ant3`. `protocol.py` now converts at the wire boundary
(`antenna_to_wire()`/`antenna_from_wire()`): everything above it - AmpState,
the API, the UI - uses 1-4 matching the amp's own display. Applied to both
directions: parsing `0x27` and `0x1A`, and building Bypass (`0x0E`/`0x02`),
tuning-record request (`0x0F`), delete-segment (`0x12`) and erase-specific-
antenna (`0x10`). **The command direction is an assumption (per Tyler), NEEDS
REAL-HARDWARE VERIFICATION** - e.g. Bypass while on A2 should stay on A2.
Band numbers are *not* shifted: `0x27` reported band `6` with 14200-14300 kHz
borders, i.e. 20m, which is 1-based per the doc's band list. Open clue: before
this fix, an amp-snapshot tuning-matrix run came back empty and its last
`0x1A` reply was exactly one less than the last request on *both* antenna
(3 vs 4) and band (10 vs 11) - the antenna half is explained by this fix;
whether `0x1A`'s band field is also 0-based is unresolved (one sample, and
that record's frequencies didn't match either reading).

**Note on antenna numbering range:** antenna *numbers* are referenced as
`[1..10]` all over the ATU-management/tuning-record commands (`0x0E`/
`0x0F`/`0x10`/`0x12`) and in the `0x27` status message — that's just the
protocol's generic field width (consistent with the 10AS selector
supporting up to 10 positions in general). **Not relevant to this build:**
the target hardware (04AT/06AT tuner) only has 4 antenna ports, which
matches the 4 slots in the name/assignment tables (`0x18` get / `0xA0` set,
`0x1B` get / `0x85` set) exactly. Only antennas 1–4 need to be supported —
no need to chase antennas 5–10.

## Status/telemetry messages (amplifier → computer)

Each has a unique address byte in the `0x55 [Addr] [Len] ... [CHKSUM]`
frame. Key ones:
- `0x01` error reply to malformed message; `0x02` request-specific-message
  echo; `0x03`/`0x04` OK/ERR acks
- `0x11`–`0x14` system config (PCB versions, serial number, user sign)
- `0x15` Hello message
- `0x16` **ATU parameters** (type `0x21`=04AT / `0x41`=10AS, HW/FW
  versions, serial number)
- `0x17` ASEL parameters (obsolete, folded into `0x16`)
- `0x18` antenna assignments, get (type + band-high/low per antenna 0–3) —
  `0xA0` is the computer→amplifier write counterpart, see the Antenna
  naming/switching section above
- `0x21` error codes bitfields (many warning/soft-fault/hard-fault bits,
  incl. ATU comm errors, tuning failures, power-switch alarms). All ~130
  documented codes are now in `protocol.py`'s `FAULT_CODE_NAMES` (was
  previously only the ~35 "most common" ones, found insufficient once a
  user actually hit an unlisted code live). **Confirmed erratum in the
  protocol doc itself** (section 3.7.1): the "Error Codes 9" group (bytes
  21/22, bias-voltage-threshold warnings) is printed with the *same*
  `0x80`-`0x8F` hex labels as the "Error Codes 8" group right above it,
  instead of the correct `0x90`-`0x9F` - confirmed because this backend's
  own `active_bits()` bit-numbering (`word_idx * 16 + bit`) matches the
  doc's printed labels for every other group (e.g. "Error Codes A" ↔
  `0xA0`-`0xAF`) and only disagrees here. `FAULT_CODE_NAMES` uses the
  mathematically-correct `0x90`-`0x9F`, not the doc's typo. Also:
  `0x8C`-`0x8F` (same byte as `0x8A`/`0x8B`) are genuinely blank/undescribed
  in the doc - not a transcription gap. **Update (2026-10-23 logs):** the
  amp shows `0x8E` on its own screen (0x2F alarm byte) for ~5s while
  running a tune cycle (mode `0x8X`, ATAC) with RF present, then clears
  it itself; `0x14` (also undocumented) did the same on a low-drive tune.
  **`0x8E` = "TUNED SWR < 1.3"** - confirmed by Tyler reading the amp's
  own screen while it was set. **`0x82`** is likewise reused: the doc says
  "AMP-ATU COMMUNICATION ERROR", but the amp raises it for ~5s every time
  `0x27`'s label turns "Settings Outdated" while its screen reads "ATU
  tuning outdated - you may refresh it" (2026-10-27), so it's named that. It's the tune *result*, not "in progress":
  the 06AT tunes in ~0.5s of RF, `0x8E` appears as the RF drops and lasts
  ~5s, while the `0x27` "Matched" label lags ~4s behind. `0x14` (one
  sighting, 2W drive) is named "ATU TUNING NOTICE" - likely another
  tune-result message, wording unknown. Key
  behavior: the `0x21` bit for `0x8E` **stays set for minutes** after the
  screen clears it, so `0x21` is not a "what's wrong right now" signal -
  the dashboard's warning banner now uses 0x2F's live alarm code
  (`protocol.alarm_name()`, `0xFF` = none) instead.
- `0x2A` "ATU Service" menu parameters (model, versions, working hours,
  tune % for C/L/CA, serial number)
- `0x2E`/`0x2F` **main cyclic telemetry** — the one to poll/stream for a
  live dashboard. `0x2F` (72 bytes) includes: amplifier mode, two 16-bit
  flag words (bias/HV/relay/key states, fault-disable flags, LPF tuned,
  ORC), DC power consumption, system clock, PAM temperature, **input
  power, forward power, reflected power, SWR (×100)**, dissipation power,
  VCC5/VCC26/HV1/HV2, drain currents, carrier frequency [kHz], per-stage
  bias voltages, active alarm code, fan speed, active LPF channel, and a
  system-status byte (default antenna selected, ATU assigned, etc.)
- `0x31`–`0x34` Hard Fault event records (4 messages, saved snapshot of
  amp state at fault time)
- `0x82`–`0x86` are actually **computer → amplifier** messages (address
  range continues straight from the `0x81` command envelope): `0x82`
  amplifier serial number, `0x83`/`0x84` Hello message (split in two, chars
  0–14 then 15–29), `0x85` set antenna names (see above), `0x86` ACK
  (Byte3 = number of message being acknowledged)
- `0x90` obsolete HEX↔ASCII mode switch command (removed), `0x91` disable
  automatic telemetry, `0x92` enable automatic telemetry, `0x93` bootloader
  activation (firmware update entry point — handle with care)

## Open questions / unknowns

1. Does the **06AT/20AT** use the same `0x16`-style ATU messages and `0x0E`
   ATU-management commands as the 04AT/10AS, or an extended protocol? The
   06AT/20AT manual gives no byte-level answer — only GUI (ACOM Terminal S)
   procedures.
2. Is the wire protocol for the **1200S** (2024 manual) identical to the
   **A1200S** (2018 protocol doc), or has it changed? The 1200S manual
   punts to "contact your dealer" for the protocol — this should be
   requested from ACOM/dealer to confirm or get an updated spec.
3. No command found yet for **direct antenna selection by index for normal
   tuned operation** — only `0x09`/`0x30` "Next Antenna" (cycle, no reverse)
   and `0x0E`/`0x02` "Bypass Command (Number of Antenna)" in Byte4, which
   selects an antenna directly but bypasses the ATU/tuning. (Antenna
   numbering range is a non-issue for this build — only 4 ports exist on
   the target 04AT/06AT hardware, matching the 4-slot name/assignment
   tables exactly.)
4. Pin 6 (DSR) vs pin 8 (CTS) both documented as "Remote Power On" on the
   1200S RS-232 connector. The 1200S manual (8.2(c)) confirms DSR/DTR +
   CTS/RTS together give "Remote POWER ON" only (no RS232-side power-off
   exists — that's a separate software/internal procedure, see item 5
   below). On the real amp, a DTR+RTS pulse of ~1.5s (both lines asserted
   together, then released) reliably **toggles** power state (on→off or
   off→on, whichever it wasn't) — holding both continuously asserted
   instead leaves the amp stuck mid power-sequence.

   **Same-session test matrix (2026-10-14) distinguishes two different
   things — resting line level vs. a deliberate timed pulse — with
   opposite results:**
   - *Resting level during routine port open/close* (what `serial_link.py`
     controls): pyserial's default (assert both HIGH on open), explicit
     deassert (LOW), and fully untouched (no ioctl at all, HUPCL cleared
     so close() can't drop them either) were all tried as the open/close
     behavior. The power-off hang (item 5) reproduced identically in
     **all three**, and a cable replug while the amp was off still
     powered it ON regardless of which level the lines rested at. So no
     *resting* level choice on these lines prevents or fixes anything —
     `serial_link.py`'s `_open_port()` now just avoids actively driving
     them on open/close, which is harmless but also not a fix for
     anything by itself.
   - *A deliberate standalone pulse* (assert both HIGH, hold ~1.5s,
     explicitly release to LOW, close) — run as a one-off action
     independent of the port's normal open/close lifecycle, bypassing
     `serial_link.py` entirely — **did recover an amp hung mid power-off**
     (comms_ok was false beforehand; confirmed clean power-down after).
     This matches the original documented toggle mechanism exactly, and
     is a genuine candidate recovery action, not just a toggle-while-
     already-working trick — see item 5's update. Still unverified:
     minimum pulse width, and whether DSR and CTS matter independently or
     only together. Given this, **don't remove pins 6/8 from the cable**
     (an earlier version of this note suggested that as the fallback) —
     they're the only confirmed way to recover from item 5's hang without
     a full Pi power cycle. Not yet explored: whether a Pi-side USB port
     power-cycle (`uhubctl` or similar, if the hub supports per-port
     switching) could stand in for a true unplug/replug as an alternative
     recovery action — raised as an option, not pursued, now lower
     priority given the pulse itself works.

   **Correction (2026-10-23): "untouched" was never actually untouched.**
   The Linux tty layer asserts DTR+RTS on *every* open() of the port,
   before pyserial runs, so `_open_port()`'s old "avoid driving the lines
   at all" behavior really meant "leave both asserted from open onward" -
   and with HUPCL cleared they stayed asserted across close() and
   restarts too. A service restart from `install.sh` (logs: only event on
   the lines was the port open at restart, no pulse) was followed by the
   amp locking up, consistent with this item's own "holding both
   continuously asserted leaves the amp stuck" finding. Fixed in
   `serial_link.py`: `_release_control_lines()` now drives both LOW right
   after every open (kernel assertion shrinks to a few ms), in a `finally`
   at the end of `pulse_power_toggle()` (so a cancelled pulse can't leave
   them held), and in `stop()` before close. Invariant: outside a
   deliberate pulse, DTR/RTS are always released. NEEDS REAL-HARDWARE
   VERIFICATION that the few-ms kernel blip on open isn't itself read as a
   toggle, and that a service restart no longer locks the amp.
5. The amp also supports a documented software-driven power-off
   (`FLAG_PwrOff_Request`/`FLAG_Soft_PwrOff` in `0x2F` telemetry flags,
   and mode category `0xAX` "Turn OFF Procedure"), separate from the
   RS232 power-on pins. On real hardware, commanding `0x81` Cmd `0x02`
   Byte5=`0x0A` (turn off) hangs the amp mid-"Turn OFF Procedure" every
   time it's been tried. Confirmed **not** a backend bug:
   `_send_pulse_command()` in `app.py` sends this command with
   `max_attempts=1` (no retry/resend; journal shows exactly one
   `02 00 0a` frame per attempt) — so it's a genuine firmware quirk in
   the amp's own internal shutdown sequence when triggered via RS232,
   not something the Pi is doing wrong on the way in. **Recovery
   confirmed**, though: a standalone DTR+RTS pulse (assert ~1.5s, release
   — see item 4's update) cleanly powered the amp down from the hung
   state, no Pi reboot or physical unplug/replug needed. Not yet
   implemented anywhere in `acom-control` — tested only as a one-off
   script run directly against the port, outside the running service.
   Candidate next step: expose this as an explicit, confirmation-gated
   "force power cycle" recovery action (unlike `_open_port()`'s routine
   open/close, this would be a deliberate, user-triggered pulse) — design
   not yet decided. Also worth a frontend warning on the "Power Off"
   button that it's known to hang the amp on real hardware every time.

   **Stronger result, same session:** the pulse was then tried as the
   *primary* power-off action from a normal running/responsive state (no
   `0x0A` command involved at all, not recovering from any hung state) —
   clean shutdown, confirmed on the amp's own display. Running tally: the
   pulse has worked cleanly **4/4** times (2x recovering an already-hung
   amp, 2x direct power-off from normal operation, including one case
   that was two pulses back-to-back toggling off→on→off, still clean each
   time), vs. the `0x0A` software command hanging **100%** of attempts.
   This reframes the pulse from "recovery mechanism" to a plausible
   *replacement* for the Power Off button's command entirely — sidesteps
   the firmware bug rather than cleaning up after it. Still a **blind
   toggle**, not a deterministic "off" — pulsing while the amp is already
   off would turn it back on, so any implementation needs to not fire
   this when the current state is uncertain.

   **Implemented:** `SerialLink.pulse_power_toggle()` (asserts DTR+RTS,
   sleeps `POWER_PULSE_SECONDS` = 1.5s, releases) is now what `POST
   /api/power` with `on: false` actually does, replacing the `0x0A`
   command entirely. Guarded against the blind-toggle risk by refusing
   (`409`) unless `amp_state.comms_ok` is currently true — without fresh
   comms we can't tell a pulse-to-off apart from an accidental pulse-to-
   on. `_open_port()`'s DTR/RTS neutering (item 4) only suppresses the
   open()-time ioctl now, not the methods permanently - restored right
   after `open()` so a deliberate pulse later still reaches the hardware.
   **Confirmed working end-to-end on real hardware** (through the actual
   button/service, not just the standalone script).

   **Power On needed the same treatment, for the opposite reason:**
   real-hardware testing found `MODE_OPR_RX` does nothing from a
   confirmed-off state - it's a serial command, which needs the amp's own
   CPU/UART already powered and listening to parse it (same reason the
   `0x0A` off command could never have turned it back on either). Only
   the pulse can wake a fully-off amp. But naively pulsing whenever
   `comms_ok` is false would be wrong the other direction: comms down
   means hung/cable-pulled/faulted just as often as it means off, and
   pulsing a merely-hung-but-still-powered amp would turn it OFF. So
   `AmpState` now tracks `power_state` (`"unknown"`/`"on"`/`"off"`)
   deliberately rather than inferring it from `comms_ok`: `"off"` is set
   *only* by our own comms-confirmed pulse (`mark_power_off_confirmed()`),
   never guessed from comms merely going quiet; `"on"` is set by any live
   telemetry; losing comms from `"off"` stays `"off"` rather than
   regressing to `"unknown"`. `set_power()`'s `on: true` branch now pulses
   if `power_state == "off"`, keeps the `MODE_OPR_RX` command if `"on"`
   (useful for STB→OPR/RX), and refuses (`409`) if `"unknown"` rather than
   guessing. **Confirmed working end-to-end on real hardware** (Power Off
   pulse through the actual button; a subsequent Power On click also
   fired a pulse as expected).

   **"Unknown" dead end, found live and fixed:** real-hardware testing hit
   exactly the gap this design implies - comms lost while `"on"`
   (ambiguous: hung/cable/fault/off?) correctly degraded to `"unknown"`,
   but nothing could recover from there through the UI (both the pulse
   and mode-command branches are gated off it) - over 2 minutes of
   continuous handshake retries with zero replies, stuck. Per Tyler's
   direction: give it one more bounded ~10s window (`ASSUME_OFF_AFTER_S`
   in `app.py`) after losing comms from `"on"` (or at startup, if the amp
   was already off before comms were ever established) for a reply;
   if nothing comes back, conclude `power_state = "off"` rather than
   leaving the operator stuck - Power On's existing pulse branch then
   handles it normally. Verified end-to-end against the running backend
   (fresh boot → `"unknown"` → auto-resolves to `"off"` after ~11s of
   silence → Power On correctly pulses). NEEDS REAL-HARDWARE
   VERIFICATION: the timeout-inference path itself (as opposed to the
   pulse branches it unlocks, which are confirmed) hasn't been exercised
   against a real ambiguous-comms-loss event on the amp yet.

   **Applied:** real-hardware testing (2026-10-20) found a front-panel
   power-off took up to ~20s to show up in the UI - the comms watchdog's
   own 10s detection window (item 7 below) plus this 10s grace period,
   stacked. A DEBUG-level log from that same session showed telemetry
   (`0x2F`) actually arriving every ~120-180ms once comms are up, far
   faster than either 10s value assumed - so both were dropped to 3.0s
   together (`ASSUME_OFF_AFTER_S` here, `COMMS_HEALTHY_WINDOW_S` in
   `serial_link.py`), landing around ~6-7s worst case instead of ~20s,
   still a wide margin over one real telemetry gap. Deliberately kept
   nonzero rather than instant - an amp that's merely hung/glitched
   while still powered on needs *some* window to avoid a later Power On
   click's pulse turning it off instead (the exact failure mode this
   whole `power_state` design exists to prevent). NEEDS REAL-HARDWARE
   VERIFICATION that 3.0s/3.0s doesn't introduce false "off" flips on
   brief real-world comms hiccups - only confirmed safe by log analysis
   of the normal telemetry cadence, not by provoking an actual flaky
   link.
6. On real hardware, the generic "request specific message" mechanism
   (`0x81` Cmd `0x01`, Byte4/5 = message address — confirmed from the
   protocol doc §5.1 to be a 16-bit Byte4(HW)/Byte5(LW) value, not a
   single-byte field; the backend's encoding already matches this) got a
   reply for `0x16` (ATU/ASEL identity) but no reply for `0x18` (antenna
   assignments), `0x1B` (antenna names), or `0x2A` (ATU service info) —
   "across many attempts" turned out to mean many clicks of Antenna
   Setup's "Refresh from Amp" button in that one session, which had a
   real bug: it only called `loadSetup()` (re-reads the backend's already-
   cached `AmpState`), never actually re-asking the amp. So every click
   was silently a no-op on the wire, not a real retry — the one genuine
   attempt was the initial startup handshake, fired the instant the
   serial port opened, quite possibly before the ATU had finished
   associating with the amp. Fixed: "Refresh from Amp" now calls
   `POST /api/resync` (re-sends the `0x16`/`0x18`/`0x1B`/`0x2A` requests
   on the already-open port) before re-reading, so a later click can
   succeed even if the first attempt was too early.

   **Reconfirmed real, not a timing fluke** (same real-hardware session
   that characterized the DTR/RTS pulse, items 4/5): `0x18`/`0x1B`/`0x2A`
   still never reply even after the many comms-recovery handshakes that
   fired throughout that session (the auto-handshake-retry fix, item 5,
   re-sent the full handshake repeatedly). `atu_identity` (`0x16`) stays
   populated correctly (type/HW/FW versions) but its own `serial_number`
   field is also empty - so this isn't purely "message doesn't arrive at
   all," `0x16`'s reply itself may be missing data too.

   **New candidate tried:** `0x81` Cmd `0x0E` Byte5=`0x2E`/`0x2F`
   ("Activate Cyclic Message 0x2E/0x2F to the Display," doc §5.1's ATU
   Management table) was already in this file's protocol facts and had
   constants in `protocol.py` (`ATU_ACTIVATE_CYCLIC_2E`/`_2F`) but no code
   ever built or sent it - distinct from the blanket `0x92` "enable
   automatic telemetry" that's confirmed to keep `0x2F` (main cyclic
   telemetry) flowing on its own. Now sent as part of `_startup_handshake()`
   (`build_atu_activate_cyclic_display()`). Speculative, not confirmed -
   the doc doesn't explicitly tie `0x18`/`0x1B`/`0x2A` to this activation,
   only `0x2E`'s own note mentions a menu-display flag with similar
   "to the Display" phrasing as `0x1B`'s section title. NEEDS REAL-
   HARDWARE VERIFICATION - untested live as of this write-up.

   **Result (2026-10-23 logs): harmful, removed.** "Activate cyclic 0x2E"
   does exactly what it says - the amp switches its cyclic telemetry to
   `0x2E` ("ATU Measure") and stops sending `0x2F`. After every service
   restart (including self-updates) the amp streamed only `0x2E` + `0x2A`
   with zero `0x2F`, so the dashboard had no mode/power/alarm data and
   looked disconnected even though comms were fine; it stayed that way
   until an STB/OPR mode change (power-cycling didn't help - the next
   handshake re-sent it). That is also the *only* reason `0x2A` ever
   "replied": ATU-Measure mode streams it on its own, so it doesn't
   answer open question 6 for `0x18`/`0x1B`. The handshake now sends only
   "activate cyclic 0x2F", and `app.py`'s `_maybe_restore_main_telemetry()`
   re-sends that if `0x2E` frames arrive with no `0x2F` for 5s. NEEDS
   REAL-HARDWARE VERIFICATION that the `0x2F` activation alone switches
   the stream back (a mode change is confirmed to).

   Still unconfirmed: whether `0x18`/`0x1B`/`0x2A` have some other
   precondition (e.g. requiring the ATU to be fully assigned, or a menu
   state only reachable from the amp's own front panel) that nothing
   triable remotely satisfies - open question 1/2 (06AT/20AT vs. the
   2018 doc's 04AT/10AS, and 1200S vs. A1200S protocol differences) may
   ultimately be the real answer, in which case this needs ACOM/dealer
   clarification rather than further guessing from this doc alone.
7. Tyler wants the frontend's "Comms" status chip to act as a tighter
   watchdog - originally floated as "turn red after just ~0.5s of
   silence." The chip's LED just mirrors `comms_ok` from
   `/api/status`/`AmpState`, driven by `SerialLink._comms_watchdog()` in
   `serial_link.py` (polls once/sec, flips unhealthy after
   `COMMS_HEALTHY_WINDOW_S` of no received frame - see item 5's update
   above for the ~20s→~6-7s fix). **Partially applied:** that window
   was lowered from 10.0s to 3.0s (alongside `ASSUME_OFF_AFTER_S`) as
   part of fixing the power-off lag, which also makes the Comms chip
   itself noticeably snappier as a side effect - but 3.0s is still far
   from the originally-floated ~0.5s, and this was tuned for the
   `power_state`/pulse-vs-command safety logic (item 4/5), not
   specifically for the chip. Going all the way to ~0.5s on the shared
   backend watchdog still risks spurious `power_state` flips on brief,
   harmless gaps in a way a value tuned for "coarser, is the amp
   fundamentally there" doesn't. If ~0.5s responsiveness on the chip
   specifically still matters after trying 3.0s live, the right shape
   is probably still a **second, separate, frontend-only** liveness
   signal (the dashboard's WebSocket already gets a timestamped tick on
   every telemetry push - the frontend could track "ms since last
   message" itself) rather than lowering the shared backend watchdog
   further. No such frontend-only signal built yet.
8. **Amp-model auto-detection, implemented but NEEDS REAL-HARDWARE
   VERIFICATION:** message `0x11` ("PCB Versions of the main CPU
   module", doc §3.5.1) was previously documented above but never wired
   up in code. It's now requested at startup/resync alongside
   `0x16`/`0x18`/`0x1B`/`0x2A`, parsed by `protocol.parse_amplifier_identity()`,
   and when its Byte3 "Amplifier Type" (`1`=A600S, `2`=A1200S) maps to a
   known profile and differs from the configured `amp.model`, `app.py`'s
   `_maybe_apply_detected_amp_model()` auto-switches `config.amp.model`/
   `amp_state.profile` live (same effect as picking it in the Setup
   screen, which still works as a manual fallback - this message can't
   represent 2000S at all, and it's still open question 2 whether the
   2024 1200S's wire protocol even matches this 2018-documented byte).
   Byte-offset decoding was verified against a synthetic frame built
   directly from the doc's table, but the message itself has never been
   requested from a real amp - unconfirmed whether it replies at all to
   the generic "request specific message" mechanism, same caveat already
   open question 6 raised for `0x18`/`0x1B`/`0x2A`.
9. **Tuner-presence auto-detection, implemented but NEEDS REAL-HARDWARE
   VERIFICATION:** the Setup screen's Antenna Setup section now also
   auto-shows when live `atu_identity.present` (message `0x16`) is true,
   not just when a tuner model is manually picked - see
   `updateAntennaSetupVisibility()` in `app.js`. This relies on `0x16`'s
   `type_code` actually populating on real hardware; a comment elsewhere
   in `app.js` (`renderStatusRow`'s ATU chip) claims it "stays empty on
   real hardware for the 06AT," which would directly contradict this and
   silently fall back to manual-only (not a crash, just degraded to
   today's behavior) - this needs resolving against a real 06AT/20AT
   before trusting the auto-show behavior. Separately, per ACOM's own
   06AT/20AT manual (§6.2.1): the exact tuner model (04AT vs. 06AT vs.
   20AT) can *never* be auto-detected beyond presence - all three report
   identically as "04AT" since they share the same control board - so
   the specific-model dropdown stays a manual, cosmetic-only label by
   design, not a gap to fix later.
10. **Debug-logging toggle + log download, implemented:** `POST
   /api/config`'s new `logging_level` field (`"INFO"`/`"DEBUG"`) flips
   `logging.getLogger().setLevel()` live and persists to `config.json`,
   surfaced as a checkbox in the Setup screen. `GET /api/logs` exports
   the last 20000 lines of `journalctl -u acom-control` as a downloadable
   attachment (no app-managed log file exists - everything already went
   to journald, see "Backend vs. frontend logging" in
   `PI_CONTROL_PLAN.md`). Requires the `acom-control` system user to be
   in the `systemd-journal` group (added to `setup/acom-control.service`'s
   install comment) - NEEDS REAL-HARDWARE VERIFICATION that this
   permission is actually sufficient on the real Pi's journald config,
   not just that the endpoint runs locally. Also added explicit log
   lines for comms-restored (`amp_state.mark_comms_ok()`), power-off-
   confirmed (`amp_state.mark_power_off_confirmed()`), and DTR/RTS pulse
   release (`serial_link.pulse_power_toggle()`) so shutdown/power-on
   timing can be reconstructed from an exported log - previously only
   the *start* of each transition was logged, not when it resolved.
   Caveat for anyone analyzing a future log export: the comms watchdog's
   10s/1s granularity (item 7 above) means the derived `comms_ok`/
   `power_state` log lines can lag the real wire event by up to ~10s -
   with DEBUG on, the raw RX frame timestamps are the actual ground
   truth for "when did telemetry really stop/resume," not these derived
   transition logs.

   **`systemd-journal` permission CONFIRMED working** (item 10's one
   remaining real-hardware gap): a real `GET /api/logs` download
   (`acom-control-20261020-130018.log`, 20000 lines spanning ~14 hours)
   was pulled from the Pi and read successfully, so the `acom-control`
   user's `systemd-journal` group membership is sufficient in practice,
   not just locally.
11. **Fixed real bug, found from that same log export: `POST /api/power`
   (off) could blindly re-pulse and desync `power_state` from reality.**
   `set_power()`'s `on: false` branch only guarded on `amp_state.comms_ok`,
   never on `power_state` itself. `comms_ok` is only updated by the 1s/10s
   watchdog (item 7), so right after a successful off-pulse it can still
   read stale-`True` for up to ~10s. The log showed exactly this: two
   `POST /api/power` (off) calls 8s apart, each logging "Power state
   confirmed OFF" - the second call passed the `comms_ok` guard despite
   `power_state` already being `"off"` from the first, and would have
   blindly pulsed again (which, since the pulse is a blind toggle, risks
   turning the amp back ON while the backend kept reporting "off").
   Fixed in `app.py`'s `set_power()`: the off-branch now checks
   `power_state` first (mirroring the on-branch's existing symmetric
   check) and is a no-op if already `"off"`, instead of falling through
   to `comms_ok`. NEEDS REAL-HARDWARE VERIFICATION: specifically, that a
   rapid double Power-Off (or a frontend retry) within ~10s of the first
   now returns `{"pulsed": false}` and leaves the amp off rather than
   toggling it back on.
12. **Not a bug, ruled out:** that same `acom-control-20261020-130018.log`
   showed "Comms down - re-sending startup handshake" firing every 5s
   continuously for ~14 hours (Oct 19 22:53 through the Oct 20 12:54
   restart), which initially looked like a wedged-port failure the
   watchdog couldn't recover from on its own, since `_read_loop()`'s
   reopen path only triggers on an actual `OSError`/`SerialException`
   and none occurred. **Per Tyler: the amp was deliberately powered off
   for that entire stretch**, so zero bytes received the whole time is
   exactly the expected behavior, not evidence of anything wedged - the
   handshake resend is correctly a no-op until the amp is powered back
   on and starts replying, same as item 6's chicken-and-egg case already
   describes. A speculative `_comms_watchdog()` escalation (forcing a
   full port close/reopen after 60s of silence) was tried and then
   reverted for this reason - it would have made the watchdog cycle the
   port every 60s indefinitely any time someone legitimately leaves the
   amp off for a while, which is normal usage, not a fault condition. No
   change made in the end; `serial_link.py` only kept a harmless, pure
   refactor (`_reopen_port()` extracted from `_read_loop()`'s existing
   reopen code, identical behavior).
13. **Real-hardware fault `0x72` seen active, undocumented - not a
   backend bug.** A log capture's single `0x21` (error flags) frame
   decoded to word 7 (`0x70`-`0x7F`) = `0x0005`, i.e. bits `0x70` and
   `0x72` both active simultaneously. `0x70` is documented ("CAT
   ERROR" - the amp's CAT/rig-control interface, not this backend's
   own RS-232 link); `0x71`-`0x7F` are otherwise blank in the protocol
   doc itself (`protocol.py`'s own comment: "only 0x70 documented"),
   same situation as the already-known `0x8C`-`0x8F` gap. So
   `FAULT_CODE_NAMES.get(c, f"Unknown fault 0x{c:02X}")` showing
   "Unknown fault 0x72" in the UI is correct, honest behavior, not a
   lookup-table gap to fix - there's no name for this bit anywhere in
   the one spec document this project has. Notable, unconfirmed clue:
   it fired at the exact same instant as the documented CAT ERROR
   (`0x70`) - worth keeping in mind that `0x72` might be CAT/rig-
   control related too, but that's speculation from a single
   co-occurrence, not confirmed.
14. **CAT/rig-control config (read + write), implemented but NEEDS
   REAL-HARDWARE VERIFICATION.** Prompted by Tyler wanting to see/
   change the amp's rear-panel CAT port setup (his radio: Yaesu
   FTdx101D) from the Setup screen instead of the amp's own front
   panel. Message `0x12` ("Settings of the Amplifier", doc §3.5.2) is
   the read side - reports current CAT Interface Type/Command Set/
   Baud Rate/Byte Spacing/Polling Time (plus User Flags/buzzer volume,
   included in `amp_state.to_dict()`/the amp-snapshot download, not
   yet exposed in the CAT/Rig Control UI itself) - now requested at
   startup/resync
   alongside `0x11`/`0x16`/`0x18`/`0x1B`/`0x2A`. `0x05`/`0x06`/`0x07`
   (doc §5.1) are the write side, already listed in this file's
   protocol facts but never built until now - `protocol.py` gained
   `build_cat_settings_1/2/3()`, exposed via `POST /api/cat-settings`.
   Synthetic-frame round-trip (parse → rebuild → re-parse) confirmed
   internally consistent, but that only proves the code agrees with
   itself, not with a real amp.

   **Known gap, not a bug:** the doc's §5.1 write-command table only
   *names* Command Set values 0-5 (None/ICOM/Yaesu FT-450 family/
   Yaesu FT-817+897 family/Yaesu FT-1000MP family/Elecraft K3+Kenwood
   TS-480) of the full 4-bit range (0-15) - values 6-15 are valid and
   settable, just unlabeled in the only spec document this project
   has. The FTdx101D isn't named anywhere in this 2018 doc (it's a
   newer radio) - the amp's own front-panel menu almost certainly has
   a proper name for it, this app can't. Reading back whatever's
   *currently* configured (via `0x12`) at least shows which numeric
   slot is already working, even without a name for it.

   **Unconfirmed byte-order wrinkle:** the status reply (`0x12`)
   describes CAT Settings 1 as Byte3=Low Byte then Byte4=High Byte;
   the write command (`0x05`) describes Byte4=HW then Byte5=LW -
   opposite ordering conventions, by name, for what should be the same
   16-bit value. Implemented trusting each in isolation (matches
   `build_request_message`'s own established HW-then-LW convention for
   the write side) - NEEDS REAL-HARDWARE VERIFICATION that a value
   written via `0x05` actually reads back correctly via `0x12`, which
   would settle whether this wrinkle is real or just inconsistent doc
   terminology for the same layout.
15. **Full per-antenna/per-band ATU tuning matrix + Hard Fault records,
   implemented but NEEDS REAL-HARDWARE VERIFICATION - neither ever
   exercised against a real amp.** A protocol-coverage audit (prompted
   by Tyler wanting "every single bit of data we can get" in the amp-
   snapshot download) found two readable, documented data sources the
   backend had either underused or never touched at all:

   - **ATU tuning matrix:** `build_request_atu_tuning_record()`/`0x1A`
     (doc §3.6.1) already existed as an on-demand single lookup, but
     `AmpState` only ever kept the *last* result, overwritten every
     time - the amp-snapshot download was capturing at most one
     antenna/band combo, not the full picture. `app.py` gained
     `_collect_atu_tuning_matrix()`, which loops all 4 antennas x 11
     bands (44 combos), sending each request and polling for the
     amp's own self-identifying reply (`0x1A`'s own
     `antenna_number`/`band_number` fields) before moving to the next
     - safe to fire this way *because* the reply self-identifies,
     unlike Hard Fault records below. No reply within ~300ms is
     treated as "no stored tuning for this combo," not an error.
     Expected to take 10+ seconds end to end - that's the 44 paced
     round-trips, not a hang.
   - **Hard Fault event records (`0x31`-`0x34`, doc section 4):**
     completely unimplemented before this - `protocol.py` gained all
     four parsers (`parse_hard_fault_record_1..4`) plus
     `build_request_hard_fault_record()`, wired into `_on_frame` and a
     new `_collect_hard_fault_records()` collector. **Interpretation
     call, not explicitly confirmed by the doc text:** treated this as
     *one* stored fault event (most recent only) split across four
     messages because a single 72-byte frame can't hold it all (the
     doc's own section headers are literally "Record 1".."Record 4",
     and the request's "Byte 5 – Number of HARD FAULT event record"
     is read as selecting which of the four *messages* to request,
     not selecting among multiple historical events) - the doc never
     explicitly rules out the alternative reading (multiple stored
     events, one message-set each). `AmplifierIdentity.hard_fault_count`
     (from `0x11`) is a lifetime total either way, not a count of
     retrievable records.

   Both are on-demand only (not added to `STARTUP_REQUEST_MESSAGES` -
   too slow/unnecessary for every normal dashboard session), triggered
   solely by `GET /api/amp-snapshot`. Synthetic-frame round-trips
   (hand-built directly from the doc's own byte tables, parse → assert
   every field) passed for all four Hard Fault records and the request
   builder - confirms the code's internal consistency with its own
   reading of the doc, not with a real amp.

## Status

Protocol reverse-engineering is captured above; the backend/frontend built
from it lives in `acom-control/` (see `PI_CONTROL_PLAN.md`'s Status
section for what's implemented and what still needs real-hardware
verification). Open item: the full 06AT/20AT tuner/switch command set is
still unconfirmed — see open question 1 — likely needs a request to
ACOM/dealer per the 1200S manual's own "contact your dealer" note.

## Working across sessions: frontend/backend split

Day-to-day work on `acom-control/` is usually split across two Claude Code
sessions open in this repo at the same time, each taking one of two roles:

- **Frontend expert** — owns `acom-control/frontend/` (the dashboard's
  HTML/CSS/JS, the ACOM/Classic/Modern themes, the gauge widgets). Doesn't
  edit `acom-control/backend/` or this file's protocol content without
  checking with the backend expert first.
- **Backend expert** — owns `acom-control/backend/` (the FastAPI app,
  `serial_link.py`, `protocol.py`, `amp_state.py`, `modbus_server.py`) and
  is the one to ask about API shapes, telemetry field names/units, and
  protocol details (this file, `CLAUDE.md`, and `PI_CONTROL_PLAN.md`).

Two Claude Code sessions open in the same repo on the same machine
auto-discover each other via `ListAgents`/`SendMessage` — no setup needed,
that part just works. What doesn't persist on its own is the role: a brand
new session has no memory of being "the frontend expert." To get a role
back after closing a session:

- Give the session a persistent name at launch (`claude -n <name>`) or
  rename an existing one (`/rename <name>`). Re-launching with the same
  name resumes that session's own history — including whatever role you
  told it earlier — instead of starting fresh. (Names used so far are
  whatever's memorable at the time — e.g. "FrontEndFrank" — not pinned to
  a fixed scheme.)
- If a session really is starting fresh (new name, or someone else picking
  up a role), just point it at this section plus whichever half of
  `acom-control/` it owns — that's enough context to resume the role cold.

When one role needs something the other owns — an API shape, a field name,
a protocol detail — ask over `SendMessage` rather than guessing or editing
across the ownership line directly.
