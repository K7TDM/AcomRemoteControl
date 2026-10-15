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
  messages, `0xFF` assign/unassign ATU
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
  incl. ATU comm errors, tuning failures, power-switch alarms)
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
   1200S RS-232 connector. **Partially resolved against real hardware**:
   the 1200S manual (8.2(c)) confirms DSR/DTR + CTS/RTS together give
   "Remote POWER ON" only (no RS232-side power-off exists — that's a
   separate software/internal procedure, see item 5 below). On the real
   amp, a DTR+RTS pulse of ~1.5s (both lines asserted together, then
   released) reliably **toggles** power state (on→off or off→on,
   whichever it wasn't) — holding both continuously asserted instead
   leaves the amp stuck mid power-sequence. Critically: Linux raises DTR
   as a side effect of simply opening *any* serial port, before any
   application code runs — so **just opening `/dev/acom-amp` for normal
   communication risks toggling the amp's power** regardless of what the
   Python code does with `.dtr`/`.rts` afterward. `acom-control`'s
   `serial_link.py` deliberately never sets `.dtr`/`.rts` at all, to avoid
   compounding this, but the OS-level open-time raise is still a risk on
   every backend start/restart/crash-recovery. The only confirmed-reliable
   fix is physical: don't wire pins 6/8 in the Pi↔amp cable at all (they're
   only needed for this optional feature, not the TX/RX/GND data link) —
   not yet done. Still unverified: minimum pulse width, and whether DSR
   and CTS matter independently or only matter together.
5. The amp also supports a documented software-driven power-off
   (`FLAG_PwrOff_Request`/`FLAG_Soft_PwrOff` in `0x2F` telemetry flags,
   and mode category `0xAX` "Turn OFF Procedure"), separate from the
   RS232 power-on pins. On real hardware, commanding `0x81` Cmd `0x02`
   Byte5=`0x0A` (turn off) once caused the amp to hang mid-"Turn OFF
   Procedure" (required unplugging/replugging the USB adapter to
   recover) — unclear yet whether that's a timing issue, a firmware
   quirk, or an interaction with the DTR/RTS behavior above.
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
   succeed even if the first attempt was too early. Still unconfirmed
   until re-tested on real hardware: whether a later retry actually gets
   a reply, or whether `0x18`/`0x1B`/`0x2A` have some other precondition
   (e.g. requiring the ATU to be fully assigned) that a same-session retry
   doesn't satisfy either.

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
