"""ACOM amp serial protocol: framing/checksum, 0x81 command builders, and
per-address message parsers.

Byte offsets below are transcribed directly from
`documentation/ACOM A600S A1200S Serial Protocol V1.3.pdf` (v1.3, 2018,
Nikolay Nenov) - CLAUDE.md is a summary of this file's ground truth, not
the other way around. Where the doc is ambiguous or marks something
obsolete, that's called out in a comment at the point of use.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

START_BYTE = 0x55

# =============================================================================
# Framing (doc section 1)
# =============================================================================


def checksum(frame_without_checksum: bytes) -> int:
    """Byte(N) = 256 - (SUM(Byte0..Byte(N-1)) & 0xFF)."""
    return (256 - (sum(frame_without_checksum) & 0xFF)) & 0xFF


def build_frame(address: int, data: bytes) -> bytes:
    """Length byte = total frame length including start byte and checksum,
    i.e. len(data) + 4 (start + address + length + checksum)."""
    length = len(data) + 4
    body = bytes([START_BYTE, address & 0xFF, length & 0xFF]) + data
    return body + bytes([checksum(body)])


@dataclass
class Frame:
    address: int
    data: bytes  # payload only - excludes start/address/length/checksum
    raw: bytes


MAX_MESSAGE_LENGTH = 72  # doc section 1: "Max length of the messages is 72 bytes"


def try_parse_frame(buf: bytes) -> tuple[Optional[Frame], bytes]:
    """Attempts to parse one frame from the front of `buf`.

    Returns (frame_or_None, remaining_buf):
    - (Frame, rest) on success - `rest` is everything after the frame.
    - (None, buf) if there aren't enough bytes yet for a full frame (caller
      should wait for more data before calling again).
    - (None, buf[1:]) if the leading byte isn't a valid frame start (bad
      start byte, bad length, or bad checksum) - caller should re-call
      immediately to resync on the next byte.
    """
    if not buf:
        return None, buf
    if buf[0] != START_BYTE:
        return None, buf[1:]
    if len(buf) < 3:
        return None, buf
    length = buf[2]
    if length < 4 or length > MAX_MESSAGE_LENGTH:
        # A corrupted length byte (e.g. line noise) must not be allowed to
        # stall the parser waiting for an implausible number of bytes -
        # resync immediately instead.
        return None, buf[1:]
    if len(buf) < length:
        return None, buf
    frame_bytes = buf[:length]
    body, chk = frame_bytes[:-1], frame_bytes[-1]
    if checksum(body) != chk:
        return None, buf[1:]
    return Frame(address=frame_bytes[1], data=frame_bytes[3 : length - 1], raw=frame_bytes), buf[length:]


# =============================================================================
# 0x01 / 0x03 / 0x04 - command confirmation replies (amp -> computer)
# =============================================================================


@dataclass
class CommandAck:
    ok: bool
    param: int = 0  # 16-bit "Return Parameter" (Byte3 high / Byte4 low) - 0 for 0x01, which carries none


def parse_ok(frame: Frame) -> CommandAck:
    """Parses message 0x03 (doc section 3.3) - amp confirms a command."""
    param = (frame.data[0] << 8) | frame.data[1] if len(frame.data) >= 2 else 0
    return CommandAck(ok=True, param=param)


def parse_err(frame: Frame) -> CommandAck:
    """Parses message 0x04 (doc section 3.4) - amp rejects a command."""
    param = (frame.data[0] << 8) | frame.data[1] if len(frame.data) >= 2 else 0
    return CommandAck(ok=False, param=param)


def parse_malformed_reply(_frame: Frame) -> CommandAck:
    """Parses message 0x01 (doc section 3.1) - amp indicates our last
    message was malformed. Fixed 4-byte frame, carries no data/parameter."""
    return CommandAck(ok=False, param=0)


# =============================================================================
# 0x81 command envelope (doc section 5.1) - computer -> amplifier
# =============================================================================

CMD_EMPTY = 0x00
CMD_REQUEST_MESSAGE = 0x01
CMD_MODE_CHANGE = 0x02
CMD_HARD_FAULT_RECORD = 0x03
CMD_USER_FLAGS = 0x04
CMD_CAT_SETTINGS_1 = 0x05
CMD_CAT_SETTINGS_2 = 0x06
CMD_CAT_SETTINGS_3 = 0x07
CMD_CLEAR_SOFT_FAULT = 0x08
CMD_ANTENNA_BAND_CHANGE = 0x09
CMD_BUZZER = 0x0A
CMD_DUMP_LOG = 0x0B
CMD_SERVICE_MODE_TEST = 0x0C
CMD_FACTORY_RESET = 0x0D
CMD_ATU_MANAGEMENT = 0x0E
CMD_ATU_TUNING_RECORD_REQUEST = 0x0F
CMD_ATU_SERVICE = 0x10
CMD_TUNING_OUTDATED_TIME = 0x11
CMD_DELETE_SEGMENT_TUNING = 0x12

MODE_RESTART = 0x02
MODE_DEBUG = 0x03
MODE_SERVICE = 0x04
MODE_STB = 0x05
MODE_OPR_RX = 0x06
MODE_OPR_TX = 0x07
MODE_SET_SYSTEM_PARAMS = 0x09
MODE_OFF = 0x0A
MODE_PROHIBIT_TX = 0x40
MODE_ALLOW_TX = 0x80

BAND_NEXT = 0x80
BAND_PREV = 0x40
SEGMENT_NEXT = 0x20
SEGMENT_PREV = 0x10
ANTENNA_NEXT = 0x30

ATU_FULL_TUNE = 0x01
ATU_BYPASS = 0x02
ATU_ACTIVATE_CYCLIC_2E = 0x2E
ATU_ACTIVATE_CYCLIC_2F = 0x2F
ATU_ASSIGN_UNASSIGN = 0xFF

FACTORY_RESET_USER_PREFS = 1
FACTORY_RESET_HOUR_COUNTER = 2
FACTORY_RESET_ERROR_LOG = 3
FACTORY_RESET_ALL = 4
FACTORY_RESET_ANTENNA_ASSIGNMENTS = 5

BUZZER_PULSE = 1
BUZZER_WARNING = 2
BUZZER_PWROFF = 3
BUZZER_SOFT_FAULT = 4
BUZZER_HARD_FAULT = 5
BUZZER_PWRON = 6

# 0x81 Cmd 0x10 "ATU Service Commands" - Byte4 selection (doc section 5.1)
ATU_ERASE_CURRENT = 0x01
ATU_ERASE_SPECIFIC_ANTENNA = 0x02
ATU_ERASE_ALL = 0x04
ATU_ERASE_WHOLE_MEMORY = 0x08


def build_amp_command(cmd: int, param_hw: int = 0, param_lw: int = 0, buttons: int = 0) -> bytes:
    """0x81 envelope: [Cmd][Param HW][Param LW][Buttons].

    NOTE: "Param HW"/"Param LW" are the doc's own generic names for
    Byte4/Byte5 - for every sub-command we actually use, the real payload
    lives in Byte5 (param_lw) with Byte4 (param_hw) either unused or a
    secondary parameter (e.g. ATU bypass's antenna number). This matches
    the doc's own per-command tables, not a HW/LW split of one 16-bit value.
    """
    data = bytes([cmd & 0xFF, param_hw & 0xFF, param_lw & 0xFF, buttons & 0xFF])
    return build_frame(0x81, data)


def build_request_message(message_address: int) -> bytes:
    return build_amp_command(CMD_REQUEST_MESSAGE, param_lw=message_address)


def build_mode_change(mode: int) -> bytes:
    return build_amp_command(CMD_MODE_CHANGE, param_lw=mode)


def build_band_change(action: int) -> bytes:
    return build_amp_command(CMD_ANTENNA_BAND_CHANGE, param_lw=action)


def build_antenna_next() -> bytes:
    return build_band_change(ANTENNA_NEXT)


def build_atu_full_tune() -> bytes:
    return build_amp_command(CMD_ATU_MANAGEMENT, param_lw=ATU_FULL_TUNE)


# Antenna numbering: everything above this module (AmpState, the API, the
# UI) uses 1-based numbers matching the amp's own display ("A1".."A4").
# On the wire, this firmware counts antennas from 0, despite the doc's
# "[1..10]" - confirmed on a real 1200S: a 0x27 antenna byte of 3 while
# the front panel showed "A4R", and a byte of 0 (impossible under the
# doc's range) in another capture. The doc's own 0x18/0xA0 tables label
# the slots "Ant0".."Ant3", consistent with this. Commands that take an
# antenna number are assumed to use the same 0-based convention (per
# Tyler's direction) - NEEDS REAL-HARDWARE VERIFICATION for the command
# direction specifically. Band numbers are NOT shifted: 0x27 reports band
# 6 for 20m, which is 1-based per the doc's band list.
MAX_ANTENNA_NUMBER = 10  # protocol field width (10AS selector), not this build's 4 ports


def antenna_to_wire(antenna_number: int) -> int:
    """1-based antenna number (as shown on the amp) -> 0-based wire value."""
    if not 1 <= antenna_number <= MAX_ANTENNA_NUMBER:
        raise ValueError(f"antenna number must be 1..{MAX_ANTENNA_NUMBER}, got {antenna_number}")
    return antenna_number - 1


def antenna_from_wire(raw: int) -> int:
    """0-based wire value -> 1-based antenna number (as shown on the amp)."""
    return raw + 1


def build_atu_bypass(antenna_number: int) -> bytes:
    """0x0E/0x02 "Bypass Command (Number of Antenna)" - switches directly to
    the given antenna (1-based, as shown on the amp) but bypasses ATU
    tuning. This is the closest thing to a direct antenna-select command
    in this protocol version; see CLAUDE.md open question 3.
    """
    return build_amp_command(CMD_ATU_MANAGEMENT, param_hw=antenna_to_wire(antenna_number), param_lw=ATU_BYPASS)


def build_atu_activate_cyclic_display(message_address: int) -> bytes:
    """0x0E/0x2E or 0x0E/0x2F "Activate Cyclic Message 0x2E/0x2F to the
    Display" (doc section 5.1's ATU Management table) - a documented but,
    until now, never-sent command, distinct from the blanket 0x92 "enable
    automatic telemetry" (build_enable_auto_telemetry()) that's confirmed
    to keep 0x2F (main cyclic telemetry) flowing on its own. Being tried
    as a candidate unlock for CLAUDE.md open question 6 (0x18/0x1B/0x2A
    staying silent even after many resync attempts) - message_address
    must be ATU_ACTIVATE_CYCLIC_2E (0x2E) or _2F (0x2F) specifically; no
    other Byte5 value is documented for this command, and several other
    values mean something else entirely for Cmd 0x0E (e.g. 0x01 triggers
    a real ATU Full Tune) - never pass anything else here.
    """
    assert message_address in (ATU_ACTIVATE_CYCLIC_2E, ATU_ACTIVATE_CYCLIC_2F)
    return build_amp_command(CMD_ATU_MANAGEMENT, param_lw=message_address)


def build_buzzer(signal: int, volume: int) -> bytes:
    return build_amp_command(CMD_BUZZER, param_hw=signal, param_lw=volume)


def build_factory_reset(selection: int) -> bytes:
    return build_amp_command(CMD_FACTORY_RESET, param_lw=selection)


def build_request_hard_fault_record(record_number: int) -> bytes:
    """0x81 Cmd 0x03 - Byte4 "No matter", Byte5 = record number. Doc
    section 4 describes exactly one stored Hard Fault event (most
    recent only - "list of parameters... saved in non volatile EEPROM",
    singular), split across four fixed-address replies (0x31-0x34)
    because one 72-byte frame can't hold it all - each reply repeats
    the same HF Count/HF NUM header fields, and the section headers are
    literally titled "Record 1".."Record 4". NEEDS REAL-HARDWARE
    VERIFICATION: interpreting `record_number` here as 1-4 (selecting
    which of the four reply messages you want back), not as selecting
    among multiple historical fault events - the doc names the four
    reply sections "Record 1..4" to match, but never explicitly states
    there's only one stored event rather than a history of them."""
    return build_amp_command(CMD_HARD_FAULT_RECORD, param_lw=record_number)


def build_user_flags(flags: int) -> bytes:
    return build_amp_command(CMD_USER_FLAGS, param_hw=(flags >> 8) & 0xFF, param_lw=flags & 0xFF)


def _pack_antenna_band(antenna_number: int, band_number: int) -> int:
    """Byte4 bit-packing shared by Cmd 0x0F and Cmd 0x12: Bits7..4 = Antenna
    Number (1-based in, 0-based on the wire - see antenna_to_wire()),
    Bits3..0 = Band Number [1..11]."""
    return ((antenna_to_wire(antenna_number) & 0x0F) << 4) | (band_number & 0x0F)


def build_request_atu_tuning_record(antenna_number: int, band_number: int, record_number: int = 0) -> bytes:
    """0x81 Cmd 0x0F - request a specific ATU tuning record. Triggers an
    0x1A reply (see parse_atu_segment_tuning) - this is a status query, not
    a command, so callers should send it fire-and-forget (no OK/ERR reply
    to wait for), same as build_request_message."""
    return build_amp_command(
        CMD_ATU_TUNING_RECORD_REQUEST,
        param_hw=_pack_antenna_band(antenna_number, band_number),
        param_lw=record_number,
    )


def build_delete_segment_tuning(antenna_number: int, band_number: int, record_number: int = 0) -> bytes:
    """0x81 Cmd 0x12 - delete tuning of a specific segment. Destructive -
    erases one stored ATU tuning entry; callers must gate this behind
    explicit confirmation (see app.py's Antenna Setup "Danger Zone")."""
    return build_amp_command(
        CMD_DELETE_SEGMENT_TUNING,
        param_hw=_pack_antenna_band(antenna_number, band_number),
        param_lw=record_number,
    )


def build_atu_erase_memory(selection: int, antenna_number: int = 0) -> bytes:
    """0x81 Cmd 0x10 - ATU Service erase commands (erase current/specific/
    all tunings, or the whole ATU memory). Byte5 (antenna_number, 1-based)
    is only meaningful for ATU_ERASE_SPECIFIC_ANTENNA - 0 means "not
    applicable" and is sent as 0. Destructive and rarely used by design -
    gate behind explicit confirmation."""
    wire_antenna = antenna_to_wire(antenna_number) if antenna_number else 0
    return build_amp_command(CMD_ATU_SERVICE, param_hw=selection, param_lw=wire_antenna)


# =============================================================================
# Fixed-byte computer -> amplifier messages (doc section 5.6-5.9)
# =============================================================================


def build_force_hex_mode() -> bytes:
    """0x90 - doc marks this "Obsolete! (Removed)" in v1.3. Kept as a
    best-effort, non-fatal startup step: harmless if the firmware ignores
    it, and some units may still honor it. See serial_link.py."""
    return build_frame(0x90, b"")


def build_disable_auto_telemetry() -> bytes:
    return build_frame(0x91, b"")


def build_enable_auto_telemetry() -> bytes:
    return build_frame(0x92, b"")


def build_ack(message_address: int) -> bytes:
    """0x86 - Byte3 = "number of received message". The protocol has no
    separate per-message sequence id, and its retry model only tracks one
    outstanding unconfirmed message at a time, so this is assumed to be the
    address/type byte of the frame being acknowledged."""
    return build_frame(0x86, bytes([message_address & 0xFF]))


# =============================================================================
# Antenna names: get 0x1B (amp -> computer), set 0x85 (computer -> amp)
# Antenna assignments: get 0x18 (amp -> computer), set 0xA0 (computer -> amp)
# =============================================================================

NUM_ANTENNAS = 4
ANTENNA_NAME_LEN = 12

BAND_NAMES: tuple[str, ...] = (
    "160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m",
)

ANTENNA_TYPE_NAMES: tuple[str, ...] = ("Sharp", "Regular", "Wide", "Fixed")


@dataclass
class AntennaSlot:
    number: int  # 1..4
    name: str = ""
    type: int = 0  # 0 Sharp / 1 Regular / 2 Wide / 3 Fixed
    band_mask: int = 0  # bit0=160m .. bit10=6m

    @property
    def bands(self) -> list[str]:
        return [BAND_NAMES[i] for i in range(len(BAND_NAMES)) if self.band_mask & (1 << i)]

    def to_dict(self) -> dict:
        type_name = ANTENNA_TYPE_NAMES[self.type] if 0 <= self.type < len(ANTENNA_TYPE_NAMES) else "Unknown"
        return {
            "number": self.number,
            "name": self.name,
            "type": type_name,
            "bands": self.bands,
        }


def _band_mask_from_names(names: list[str]) -> int:
    mask = 0
    for name in names:
        try:
            mask |= 1 << BAND_NAMES.index(name)
        except ValueError as exc:
            raise ValueError(f"unknown band name {name!r}; expected one of {BAND_NAMES}") from exc
    return mask


def antenna_slot_from_dict(d: dict) -> AntennaSlot:
    type_name = d.get("type", "Sharp")
    try:
        type_value = ANTENNA_TYPE_NAMES.index(type_name)
    except ValueError as exc:
        raise ValueError(f"unknown antenna type {type_name!r}; expected one of {ANTENNA_TYPE_NAMES}") from exc
    return AntennaSlot(
        number=d["number"],
        name=d.get("name", ""),
        type=type_value,
        band_mask=_band_mask_from_names(d.get("bands", [])),
    )


def _encode_antenna_name(name: str) -> bytes:
    raw = name.encode("ascii", errors="replace")[:ANTENNA_NAME_LEN]
    return raw.ljust(ANTENNA_NAME_LEN, b"\x00")


def build_set_antenna_names(names: list[str]) -> bytes:
    """0x85 - computer -> amp, same 52-byte layout as the 0x1B read message."""
    if len(names) != NUM_ANTENNAS:
        raise ValueError(f"expected {NUM_ANTENNAS} antenna names, got {len(names)}")
    data = b"".join(_encode_antenna_name(n) for n in names)
    return build_frame(0x85, data)


def parse_antenna_names(frame: Frame) -> list[str]:
    """Parses message 0x1B (52 bytes: Byte3-50 = 4 names x 12 ASCII chars)."""
    names = []
    for i in range(NUM_ANTENNAS):
        chunk = frame.data[i * ANTENNA_NAME_LEN : (i + 1) * ANTENNA_NAME_LEN]
        names.append(chunk.split(b"\x00", 1)[0].decode("ascii", errors="replace").rstrip())
    return names


def parse_antenna_assignments(frame: Frame) -> list[AntennaSlot]:
    """Parses message 0x18 (16 bytes: Byte3-6 type per antenna 0-3,
    Byte7-14 band-high/band-low byte pairs per antenna)."""
    data = frame.data  # bytes 3..14 inclusive (12 bytes)
    types = data[0:4]
    slots = []
    for i in range(NUM_ANTENNAS):
        band_hi = data[4 + i * 2]
        band_lo = data[5 + i * 2]
        mask = band_lo | ((band_hi & 0x07) << 8)
        slots.append(AntennaSlot(number=i + 1, type=types[i] & 0x03, band_mask=mask))
    return slots


def build_set_antenna_assignments(slots: list[AntennaSlot]) -> bytes:
    """0xA0 - computer -> amp, confirmed write counterpart of the 0x18 read
    message (same 16-byte layout; doc explicitly says "Similar to Message
    0x18" and files 0xA0 under section 5, "Messages and commands from the
    computer to the main CPU of the amplifier")."""
    if len(slots) != NUM_ANTENNAS:
        raise ValueError(f"expected {NUM_ANTENNAS} antenna slots, got {len(slots)}")
    ordered = sorted(slots, key=lambda s: s.number)
    types = bytes((slot.type & 0x03) for slot in ordered)
    band_bytes = bytearray()
    for slot in ordered:
        band_hi = (slot.band_mask >> 8) & 0x07
        band_lo = slot.band_mask & 0xFF
        band_bytes += bytes([band_hi, band_lo])
    return build_frame(0xA0, types + bytes(band_bytes))


# =============================================================================
# 0x11 - PCB Versions of the main CPU module (amp -> computer)
# =============================================================================

AMP_TYPE_A600S = 1
AMP_TYPE_A1200S = 2

AMP_TYPE_NAMES = {
    AMP_TYPE_A600S: "A600S",
    AMP_TYPE_A1200S: "A1200S",
}


@dataclass
class AmplifierIdentity:
    type_code: int
    fw_version: int
    fw_subversion: int
    cpu_version: int
    cpu_subversion: int
    interface_a_version: int
    interface_a_subversion: int
    interface_b_version: int
    interface_b_subversion: int
    modem_version: int
    modem_subversion: int
    bootloader_version: int
    bootloader_subversion: int
    serial_number: str
    hard_fault_count: int

    @property
    def type_name(self) -> str:
        return AMP_TYPE_NAMES.get(self.type_code, f"unknown(0x{self.type_code:02X})")


def parse_amplifier_identity(frame: Frame) -> AmplifierIdentity:
    """Parses message 0x11 (30 bytes: Byte3 amplifier type, Byte4-15
    FW/CPU/Interface_A/Interface_B/Modem/Bootloader version/subversion
    pairs, Byte16-27 serial number, Byte28 Hard Fault record count)."""
    data = frame.data  # bytes 3..28 inclusive (26 bytes)
    serial = data[13:25].decode("ascii", errors="replace").rstrip("\x00")
    return AmplifierIdentity(
        type_code=data[0],
        fw_version=data[1],
        fw_subversion=data[2],
        cpu_version=data[3],
        cpu_subversion=data[4],
        interface_a_version=data[5],
        interface_a_subversion=data[6],
        interface_b_version=data[7],
        interface_b_subversion=data[8],
        modem_version=data[9],
        modem_subversion=data[10],
        bootloader_version=data[11],
        bootloader_subversion=data[12],
        serial_number=serial,
        hard_fault_count=data[25],
    )


# =============================================================================
# 0x12 - "Settings of the Amplifier" (CAT interface config, User Flags,
# buzzer volume) - amp -> computer, generated once on request (doc
# section 3.5.2). Companion to the 0x05/0x06/0x07 write commands below
# (doc section 5.1) - this is how the amp reports back what's currently
# configured; the Setup screen's CAT/Rig Control section reads this,
# shows the write commands' effect, and lets the operator change it.
# =============================================================================

CAT_INTERFACE_TYPE_NAMES = {
    0: "None",
    1: "RS232",
    2: "TTL",
    3: "BCD",
    4: "Analog",
}

# Doc section 5.1's 0x05 table only names command sets 0-5; 6-15 are valid
# (4-bit field, 0-15) but left unlabeled in the doc itself - not a
# transcription gap, same situation as several fault-code gaps already
# noted in CLAUDE.md. Whatever's already configured on a working amp
# (read back via this message) is more trustworthy than guessing a name
# for an unlabeled slot.
CAT_COMMAND_SET_NAMES = {
    0: "None",
    1: "ICOM",
    2: "Yaesu FT-450 family",
    3: "Yaesu FT-817 / FT-897 family",
    4: "Yaesu FT-1000MP family",
    5: "Elecraft K3 / Kenwood TS-480",
}

CAT_BAUD_RATE_NAMES = {
    0: "None",
    1: "1200",
    2: "4800",
    3: "9600",
    4: "19200",
    5: "38400",
    6: "57600",
}


@dataclass
class AmpSettings:
    cat_interface_type: int
    cat_command_set: int
    cat_baud_rate: int
    cat_byte_spacing_us: int
    cat_polling_time_ms: int
    user_flags: int
    buzzer_volume: int

    @property
    def cat_interface_type_name(self) -> str:
        return CAT_INTERFACE_TYPE_NAMES.get(self.cat_interface_type, f"unknown({self.cat_interface_type})")

    @property
    def cat_command_set_name(self) -> str:
        return CAT_COMMAND_SET_NAMES.get(self.cat_command_set, f"Commands Set {self.cat_command_set}")

    @property
    def cat_baud_rate_name(self) -> str:
        return CAT_BAUD_RATE_NAMES.get(self.cat_baud_rate, f"unknown({self.cat_baud_rate})")


def parse_amp_settings(frame: Frame) -> AmpSettings:
    """Parses message 0x12 (13 bytes). Byte3 = CAT Settings 1 Low (bits
    4-7 baud rate, bits 0-3 unused), Byte4 = CAT Settings 1 High (bits
    4-7 interface type, bits 0-3 command set), Byte5/6 = CAT Settings 2
    (byte spacing, us, low/high), Byte7/8 = CAT Settings 3 (polling
    time, ms, low/high), Byte9/10 = User Flags (low/high), Byte11 =
    buzzer volume."""
    data = frame.data  # bytes 3..11 inclusive (9 bytes)
    cat1_low, cat1_high = data[0], data[1]
    return AmpSettings(
        cat_interface_type=(cat1_high >> 4) & 0xF,
        cat_command_set=cat1_high & 0xF,
        cat_baud_rate=(cat1_low >> 4) & 0xF,
        cat_byte_spacing_us=data[2] | (data[3] << 8),
        cat_polling_time_ms=data[4] | (data[5] << 8),
        user_flags=data[6] | (data[7] << 8),
        buzzer_volume=data[8],
    )


def build_cat_settings_1(interface_type: int, command_set: int, baud_rate: int) -> bytes:
    """0x05 - CAT Settings 1: Byte4(HW) = (Interface Type << 4 | Command
    Set), Byte5(LW) = (Baud Rate << 4) with the low nibble unused -
    doc section 5.1 describes this as one 16-bit value (bits 15-12
    Interface Type, 11-8 Command Set, 7-4 Baud Rate, 3-0 unused) split
    across Byte4/Byte5 the same way build_request_message's message
    address is - see its own docstring. NEEDS REAL-HARDWARE
    VERIFICATION: the doc's status-reply message (0x12, above) lists
    CAT Settings 1's Low Byte first (Byte3) then High Byte (Byte4) -
    opposite of this write command's own Byte4(HW)-then-Byte5(LW)
    labeling - both are trusted at face value here since nothing
    contradicts either individually, but the two haven't been cross-
    checked against each other on a real amp yet."""
    hw = ((interface_type & 0xF) << 4) | (command_set & 0xF)
    lw = (baud_rate & 0xF) << 4
    return build_amp_command(CMD_CAT_SETTINGS_1, param_hw=hw, param_lw=lw)


def build_cat_settings_2(byte_spacing_us: int) -> bytes:
    """0x06 - CAT Settings 2: Byte Spacing [us], Byte4(HW)/Byte5(LW) of
    one 16-bit value (0..65535)."""
    value = byte_spacing_us & 0xFFFF
    return build_amp_command(CMD_CAT_SETTINGS_2, param_hw=(value >> 8) & 0xFF, param_lw=value & 0xFF)


def build_cat_settings_3(polling_time_ms: int) -> bytes:
    """0x07 - CAT Settings 3: Polling Time [ms], Byte4(HW)/Byte5(LW) of
    one 16-bit value (0..65535)."""
    value = polling_time_ms & 0xFFFF
    return build_amp_command(CMD_CAT_SETTINGS_3, param_hw=(value >> 8) & 0xFF, param_lw=value & 0xFF)


# =============================================================================
# 0x16 - ATU/ASEL identity (amp -> computer)
# =============================================================================

ATU_TYPE_NONE = 0x00
ATU_TYPE_04AT = 0x21
ATU_TYPE_10AS = 0x41

ATU_TYPE_NAMES = {
    ATU_TYPE_NONE: "none",
    ATU_TYPE_04AT: "04AT",
    ATU_TYPE_10AS: "10AS",
}


@dataclass
class AtuIdentity:
    type_code: int
    hw_version: int
    hw_subversion: int
    fw_version: int
    fw_subversion: int
    serial_number: str

    @property
    def type_name(self) -> str:
        return ATU_TYPE_NAMES.get(self.type_code, f"unknown(0x{self.type_code:02X})")

    @property
    def present(self) -> bool:
        return self.type_code != ATU_TYPE_NONE


def parse_atu_identity(frame: Frame) -> AtuIdentity:
    """Parses message 0x16 (19 bytes: Byte3 type, Byte4-7 HW/FW versions,
    Byte8-17 serial number)."""
    data = frame.data  # bytes 3..17 inclusive (15 bytes)
    serial = data[5:15].decode("ascii", errors="replace").rstrip("\x00")
    return AtuIdentity(
        type_code=data[0],
        hw_version=data[1],
        hw_subversion=data[2],
        fw_version=data[3],
        fw_subversion=data[4],
        serial_number=serial,
    )


# =============================================================================
# 0x27 - current antenna/band/segment status (amp -> computer)
# =============================================================================

ATU_LABEL_BYPASS = 6  # see ATU_LABELS below - drives the frontend's ATU Bypass button state

ATU_LABELS = {
    0: "No Label",
    1: "ATU Initialization",
    2: "Recalling Settings",
    3: "Matching Cycle",
    4: "ATU MEM Transfer",
    5: "Completed",
    6: "ATU In Bypass",
    7: "ATU Matched",
    8: "Coarse ATU Match",
    9: "No Match Found",
    10: "Settings Outdated",
    11: "Memory Settings",
}


@dataclass
class AntennaBandStatus:
    antenna_number: int
    antenna_type: int
    is_default_antenna: bool
    band_number: int
    segment_count: int
    current_segment: int
    low_border_khz: int
    high_border_khz: int
    segment_status_raw: list[int] = field(default_factory=list)  # Byte12-18, bit-level meaning unconfirmed
    atu_label: int = 0

    @property
    def atu_label_name(self) -> str:
        return ATU_LABELS.get(self.atu_label, f"unknown({self.atu_label})")


def parse_antenna_band_status(frame: Frame) -> AntennaBandStatus:
    """Parses message 0x27 (21 bytes). Generated only when band/segment
    changes, not streamed continuously."""
    data = frame.data  # bytes 3..19 inclusive (17 bytes)
    ant_type_byte = data[1]
    return AntennaBandStatus(
        antenna_number=antenna_from_wire(data[0]),
        antenna_type=ant_type_byte & 0x7F,
        is_default_antenna=bool(ant_type_byte & 0x80),
        band_number=data[2],
        segment_count=data[3],
        current_segment=data[4],
        low_border_khz=data[5] | (data[6] << 8),
        high_border_khz=data[7] | (data[8] << 8),
        segment_status_raw=list(data[9:16]),
        atu_label=data[16],
    )


# =============================================================================
# 0x21 - error/warning/fault bitfields (amp -> computer)
# =============================================================================


@dataclass
class ErrorFlags:
    words: list[int]  # 11 x 16-bit words ("Error Codes 0".."Error Codes A")

    def active_bits(self) -> list[int]:
        """Returns the flat bit numbers that are set, matching the doc's own
        (0xNN) numbering scheme for each fault/warning bit."""
        bits = []
        for word_idx, word in enumerate(self.words):
            for bit in range(16):
                if word & (1 << bit):
                    bits.append(word_idx * 16 + bit)
        return bits


# Complete table from protocol doc section 3.7.1 (bits with no description
# in the doc - mostly spare/reserved - are omitted; those still show up as
# a raw hex code to the frontend, e.g. 0x8C-0x8F are genuinely undocumented
# in the doc itself, not a gap in this transcription). Exceptions: 0x14 and
# 0x8E are named from real-hardware observation - see the note at 0x8E.
#
# CORRECTED vs. the doc's own printed labels for the "Error Codes 9" group
# (Byte 21/22, i.e. 0x90-0x9F): the doc mistakenly reprints "Error Codes 8"
# 's own 0x80-0x8F labels for every row in that group instead of 0x90-0x9F -
# confirmed by this file's own active_bits() math (word_idx*16 + bit), which
# agrees with the doc's printed labels for every OTHER word (e.g. Error
# Codes A correctly prints 0xA0-0xAF) and disagrees only here. The dict
# below uses the mathematically-correct 0x90-0x9F codes, not the doc's
# typo'd 0x80-0x8F.
FAULT_CODE_NAMES: dict[int, str] = {
    # Error Codes 0 (0x00-0x0F)
    0x00: "HOT SWITCHING ATTEMPT",
    0x01: "OUTPUT RELAY CLOSED, SHOULD BE OPEN",
    0x02: "OUTPUT RELAY OPEN, SHOULD BE CLOSED",
    0x03: "DRIVE POWER DETECTED AT WRONG TIME",
    0x04: "REFLECTED POWER WARNING",
    0x05: "EXCESSIVE REFLECTED POWER",
    0x06: "DRIVE POWER TOO HIGH",
    0x07: "EXCESSIVE DRIVE POWER",
    0x08: "HOT SWITCHING ATTEMPT",
    0x09: "DRIVE FREQUENCY OUT OF RANGE",
    0x0A: "FREQUENCY VIOLATION",
    0x0B: "OUTPUT DISBALANCE",
    0x0C: "DETECTED RF POWER AT WRONG TIME",
    0x0D: "PA LOAD SWR TOO HIGH",
    0x0E: "STOP TRANSMISSION FIRST",
    0x0F: "REMOVE DRIVE POWER IMMEDIATELY",
    # Error Codes 1 (0x10-0x1F) - 0x14 undocumented in the doc; see the
    # 0x8E note below for why it's named here anyway.
    0x10: "5V TOO LOW",
    0x11: "5V TOO HIGH",
    0x12: "26V TOO LOW",
    0x13: "26V TOO HIGH",
    0x14: "ATU TUNE NOTICE (0x14, UNDOCUMENTED)",
    0x15: "PAM1 FAN SPEED TOO LOW",
    0x16: "PAM2 FAN SPEED TOO LOW",
    0x17: "LPF FAN SPEED TOO LOW",
    0x18: "PAM1 DISSIPATION POWER TOO HIGH",
    0x19: "PAM2 DISSIPATION POWER TOO HIGH",
    0x1A: "PAM1 DISSIPATION POWER WARNING",
    0x1B: "PAM2 DISSIPATION POWER WARNING",
    0x1C: "PAM1 TEMPERATURE TOO HIGH",
    0x1D: "PAM2 TEMPERATURE TOO HIGH",
    0x1E: "PAM1 EXCESSIVE TEMPERATURE",
    0x1F: "PAM2 EXCESSIVE TEMPERATURE",
    # Error Codes 2 (0x20-0x2F)
    0x20: "PAM1 HV TOO LOW",
    0x21: "PAM1 HV TOO HIGH",
    0x22: "PAM1 CURRENT SHOULD BE ZERO",
    0x23: "PAM1 IDLE CURRENT TOO LOW",
    0x24: "PAM1 CURRENT WARNING",
    0x25: "PAM1 EXCESSIVE CURRENT",
    0x26: "BIAS_1A VOLTAGE ERROR",
    0x27: "BIAS_1B VOLTAGE ERROR",
    0x28: "BIAS_1C VOLTAGE ERROR",
    0x29: "BIAS_1D VOLTAGE ERROR",
    0x2A: "BIAS_1A SHOULD BE ZERO",
    0x2B: "BIAS_1B SHOULD BE ZERO",
    0x2C: "BIAS_1C SHOULD BE ZERO",
    0x2D: "BIAS_1D SHOULD BE ZERO",
    0x2E: "PAM1 GAIN TOO LOW",
    0x2F: "PAM1 GAIN TOO HIGH",
    # Error Codes 3 (0x30-0x3F) - 0x3A-0x3F undocumented
    0x30: "PAM1 HV SHOULD BE ZERO",
    0x31: "PAM1 CURRENT SHOULD BE ZERO",
    0x32: "PAM1 EXCESSIVE TEMPERATURE",
    0x33: "PAM1 TEMPERATURE TOO HIGH",
    0x34: "BIAS_1A SHOULD BE ZERO",
    0x35: "BIAS_1B SHOULD BE ZERO",
    0x36: "BIAS_1C SHOULD BE ZERO",
    0x37: "BIAS_1D SHOULD BE ZERO",
    0x38: "PSU1 EXCESSIVE TEMPERATURE",
    0x39: "PAM1 EXCESSIVE CURRENT - CHECK SWR OR REDUCE DRIVE POWER",
    # Error Codes 4 (0x40-0x4F)
    0x40: "PAM2 HV TOO LOW",
    0x41: "PAM2 HV TOO HIGH",
    0x42: "PAM2 CURRENT SHOULD BE ZERO",
    0x43: "PAM2 IDLE CURRENT TOO LOW",
    0x44: "PAM2 CURRENT WARNING",
    0x45: "PAM2 EXCESSIVE CURRENT",
    0x46: "BIAS_2A VOLTAGE ERROR",
    0x47: "BIAS_2B VOLTAGE ERROR",
    0x48: "BIAS_2C VOLTAGE ERROR",
    0x49: "BIAS_2D VOLTAGE ERROR",
    0x4A: "BIAS_2A SHOULD BE ZERO",
    0x4B: "BIAS_2B SHOULD BE ZERO",
    0x4C: "BIAS_2C SHOULD BE ZERO",
    0x4D: "BIAS_2D SHOULD BE ZERO",
    0x4E: "PAM2 GAIN TOO LOW",
    0x4F: "PAM2 GAIN TOO HIGH",
    # Error Codes 5 (0x50-0x5F) - 0x5A-0x5F undocumented
    0x50: "PAM2 HV SHOULD BE ZERO",
    0x51: "PAM2 CURRENT SHOULD BE ZERO",
    0x52: "PAM2 EXCESSIVE TEMPERATURE",
    0x53: "PAM2 TEMPERATURE TOO HIGH",
    0x54: "BIAS_2A SHOULD BE ZERO",
    0x55: "BIAS_2B SHOULD BE ZERO",
    0x56: "BIAS_2C SHOULD BE ZERO",
    0x57: "BIAS_2D SHOULD BE ZERO",
    0x58: "PSU2 EXCESSIVE TEMPERATURE",
    0x59: "PAM2 EXCESSIVE CURRENT - CHECK SWR OR REDUCE DRIVE POWER",
    # Error Codes 6 (0x60-0x6F) - 0x6A/0x6B undocumented
    0x60: "PSU1/CONTROL MALFUNCTION",
    0x61: "PSU2/CONTROL MALFUNCTION",
    0x62: "PSU1 EXCESSIVE TEMPERATURE",
    0x63: "PSU2 EXCESSIVE TEMPERATURE",
    0x64: "DISPLAY UNIT COMMUNICATION ERROR",
    0x65: "ATU MODEM EXCESSIVE TEMPERATURE",
    0x66: "ATU POWER SWITCH ALARM",
    0x67: "ATU POWER SWITCH ALARM AT POWER ON",
    0x68: "ETHERNET CONTROLLER NOT RESPONDING",
    0x69: "AUDIO MEMORY NOT RESPONDING",
    0x6C: "LOSS OF AUDIO DATA",
    0x6D: "LOSS OF ETHERNET SETTINGS",
    0x6E: "LOSS OF EEPROM DATA",
    0x6F: "LOSS OF EEPROM DATA",
    # Error Codes 7 (0x70-0x7F) - only 0x70 documented
    0x70: "CAT ERROR",
    # Error Codes 8 (0x80-0x8F) - 0x8C-0x8F undocumented; 0x8E named from
    # real-hardware observation (below)
    0x80: "ATU NOT RESPONDING",
    0x81: "ATU-AMP COMMUNICATION ERROR",
    0x82: "AMP-ATU COMMUNICATION ERROR",
    0x83: "ASEL NOT RESPONDING",
    0x84: "ASEL-AMP COMMUNICATION ERROR",
    0x85: "AMP-ASEL COMMUNICATION ERROR",
    0x86: "NO TUNING SETTINGS PREPARED",
    0x87: "NO ANTENNA SETTINGS PREPARED",
    0x88: "ATU CAN NOT BE RE-TUNED UNTIL RF POWER IS PRESENT",
    0x89: "ANTENNA CAN NOT BE CHANGED UNTIL RF POWER IS PRESENT",
    0x8A: "ATU TUNING CYCLE UNSUCCESSFUL",
    0x8B: "ATU MEMORY FAIL",
    # Not in the doc. Observed on a real 1200S + 06AT (2026-10-23): the amp
    # shows 0x8E on its own screen for ~5s while running a tune cycle (mode
    # 0x8X, ATAC) with RF present, then clears it on its own as the tune
    # finishes; 0x14 behaved the same way on another tune at low drive. So
    # these read as tune-procedure prompts, not faults - exact wording on
    # the amp's screen unknown.
    0x8E: "ATU TUNE NOTICE (0x8E, UNDOCUMENTED)",
    # Error Codes 9 (0x90-0x9F) - see the correction note above this dict
    0x90: "BIAS_1A SHOULD BE BETWEEN LOW/HIGH THRESHOLD",
    0x91: "BIAS_1B SHOULD BE BETWEEN LOW/HIGH THRESHOLD",
    0x92: "BIAS_1C SHOULD BE BETWEEN LOW/HIGH THRESHOLD",
    0x93: "BIAS_1D SHOULD BE BETWEEN LOW/HIGH THRESHOLD",
    0x94: "BIAS_2A SHOULD BE BETWEEN LOW/HIGH THRESHOLD",
    0x95: "BIAS_2B SHOULD BE BETWEEN LOW/HIGH THRESHOLD",
    0x96: "BIAS_2C SHOULD BE BETWEEN LOW/HIGH THRESHOLD",
    0x97: "BIAS_2D SHOULD BE BETWEEN LOW/HIGH THRESHOLD",
    0x98: "BIAS_1A SHOULD BE BELOW HIGH THRESHOLD",
    0x99: "BIAS_1B SHOULD BE BELOW HIGH THRESHOLD",
    0x9A: "BIAS_1C SHOULD BE BELOW HIGH THRESHOLD",
    0x9B: "BIAS_1D SHOULD BE BELOW HIGH THRESHOLD",
    0x9C: "BIAS_2A SHOULD BE BELOW HIGH THRESHOLD",
    0x9D: "BIAS_2B SHOULD BE BELOW HIGH THRESHOLD",
    0x9E: "BIAS_2C SHOULD BE BELOW HIGH THRESHOLD",
    0x9F: "BIAS_2D SHOULD BE BELOW HIGH THRESHOLD",
    # Error Codes A (0xA0-0xAF)
    0xA0: "ATU/ASEL DC-INPUT VOLTAGE TOO HIGH",
    0xA1: "ATU/ASEL DC-INPUT VOLTAGE TOO LOW",
    0xA2: "ATU/ASEL INTERNAL +5V TOO LOW",
    0xA3: "ATU/ASEL INTERNAL +5V TOO HIGH",
    0xA4: "ANTENNA VOLTAGE TOO HIGH: REDUCE POWER",
    0xA5: "ANTENNA VOLTAGE TOO HIGH: ATU/ASEL IS OFF DUE TO DAMAGE RISK",
    0xA6: "ANTENNA CURRENT TOO HIGH: REDUCE POWER",
    0xA7: "ANTENNA CURRENT TOO HIGH: ATU/ASEL IS OFF DUE TO DAMAGE RISK",
    0xA8: "ANTENNA REFLECTED POWER TOO HIGH: ATU/ASEL DAMAGE RISK",
    0xA9: "ANTENNA REFLECTED POWER TOO HIGH: ATU/ASEL IS OFF DUE TO DAMAGE RISK",
    0xAA: "ATU/ASEL RF-INPUT POWER TOO HIGH: DAMAGE RISK",
    0xAB: "ATU/ASEL RF-INPUT POWER TOO HIGH: TURNED OFF DUE TO DAMAGE RISK",
    0xAC: "ANTENNA SWR TOO HIGH: ATU/ASEL DAMAGE RISK (SOFT FAULT)",
    0xAD: "ANTENNA SWR TOO HIGH: ATU/ASEL DAMAGE RISK (WARNING)",
    0xAE: "ATU/ASEL TEMPERATURE TOO HIGH: TURNED OFF DUE TO DAMAGE RISK",
    0xAF: "ATU/ASEL TEMPERATURE TOO LOW: TURNED OFF DUE TO DAMAGE RISK",
}


ALARM_CODE_NONE = 0xFF  # 0x2F Byte66 value when the amp's screen shows no alarm


def alarm_name(code: int) -> Optional[str]:
    """Name for 0x2F's live "error code of the alarm condition displayed on
    the amplifier's screen" (Byte66), or None when nothing is displayed.
    Uses the same code space as the 0x21 bitfield. This is what the amp
    is showing *right now*; 0x21's bits can stay set long after the
    screen clears (seen with 0x8E: still set minutes after a tune)."""
    if code == ALARM_CODE_NONE:
        return None
    return FAULT_CODE_NAMES.get(code, f"Unknown fault 0x{code:02X}")


def parse_error_flags(frame: Frame) -> ErrorFlags:
    """Parses message 0x21 (26 bytes: 11 words of Error Codes 0..A, each
    split Low/High byte)."""
    data = frame.data  # bytes 3..24 inclusive (22 bytes = 11 x 16-bit words)
    words = [data[i] | (data[i + 1] << 8) for i in range(0, 22, 2)]
    return ErrorFlags(words=words)


# =============================================================================
# 0x2F - main cyclic telemetry (amp -> computer)
# =============================================================================

MODE_CATEGORY_NAMES = {
    0x1: "Amplifier Reset",
    0x2: "Init Mode",
    0x3: "Debug Mode",
    0x4: "Service Mode",
    0x5: "STB Mode",
    0x6: "OPR/RX Mode",
    0x7: "OPR/TX Mode",
    0x8: "ATAC Procedure",
    0x9: "Set System Parameters Menu",
    0xA: "Turn OFF Procedure",
}


def mode_name(mode_byte: int) -> str:
    if mode_byte == 0xA2:
        return "Extra Cooling State"
    return MODE_CATEGORY_NAMES.get((mode_byte >> 4) & 0xF, f"Unknown(0x{mode_byte:02X})")


def _k_to_c(deg_k_raw: int) -> float:
    return deg_k_raw - 273.15


@dataclass
class AmpTelemetry:
    mode_byte: int = 0
    flags1: int = 0  # 16-bit, Byte4 (lo) | Byte5 (hi)
    flags2: int = 0  # 16-bit, Byte6 (lo) | Byte7 (hi)
    p_dc1_w: float = 0.0
    p_dc2_w: Optional[float] = None
    system_clock_s: int = 0
    temp_pam1_c: float = 0.0
    temp_pam2_c: Optional[float] = None
    input_power_w: float = 0.0
    forward_power_w: int = 0
    reflected_power_w: int = 0
    swr: float = 0.0
    pam1_dissipation_w: float = 0.0
    pam2_dissipation_w: Optional[float] = None
    bdata_mv: int = 0
    vcc5_mv: int = 0
    vcc26_v: float = 0.0
    hv1_v: float = 0.0
    hv2_v: Optional[float] = None
    id1_ma: int = 0
    id2_ma: Optional[int] = None
    carrier_freq_khz: int = 0
    bias_1a_mv: int = 0
    bias_1b_mv: int = 0
    alarm_code: int = 0
    alarm_param: int = 0
    fan_speed: int = 0
    active_lpf_channel: int = 0
    default_antenna_selected: bool = False
    atu_assigned: bool = False
    hide_segments_scale: bool = False
    last_fault_is_hard: bool = False

    @property
    def mode_name(self) -> str:
        return mode_name(self.mode_byte)


def parse_telemetry(frame: Frame, profile) -> AmpTelemetry:
    """Parses message 0x2F (72 bytes). `profile` is an AmpProfile - PAM2
    fields are only populated when profile.has_pam2 is True, since the doc
    marks every PAM2 byte "(Not Used)" on single-PA models."""
    data = frame.data  # bytes 3..70 inclusive (68 bytes)

    def u16(i: int) -> int:
        return data[i] | (data[i + 1] << 8)

    has_pam2 = bool(getattr(profile, "has_pam2", False))
    clock_hw = u16(9)
    clock_lw = u16(11)
    fan_lpf_byte = data[66]
    status_byte = data[67]

    return AmpTelemetry(
        mode_byte=data[0],
        flags1=u16(1),
        flags2=u16(3),
        p_dc1_w=u16(5) / 10,
        p_dc2_w=(u16(7) / 10) if has_pam2 else None,
        system_clock_s=(clock_hw << 16) | clock_lw,
        temp_pam1_c=_k_to_c(u16(13)),
        temp_pam2_c=_k_to_c(u16(15)) if has_pam2 else None,
        input_power_w=u16(17) / 10,
        forward_power_w=u16(19),
        reflected_power_w=u16(21),
        swr=u16(23) / 100,
        pam1_dissipation_w=u16(25) / 10,
        pam2_dissipation_w=(u16(27) / 10) if has_pam2 else None,
        bdata_mv=u16(29),
        vcc5_mv=u16(33),
        vcc26_v=u16(35) / 10,
        hv1_v=u16(37) / 10,
        hv2_v=(u16(39) / 10) if has_pam2 else None,
        id1_ma=u16(41),
        id2_ma=u16(43) if has_pam2 else None,
        carrier_freq_khz=u16(45),
        bias_1a_mv=u16(47),
        bias_1b_mv=u16(49),
        alarm_code=data[63],
        alarm_param=u16(64),
        fan_speed=(fan_lpf_byte >> 4) & 0x0F,
        active_lpf_channel=fan_lpf_byte & 0x0F,
        default_antenna_selected=bool(status_byte & 0x08),
        atu_assigned=bool(status_byte & 0x02),
        hide_segments_scale=bool(status_byte & 0x04),
        last_fault_is_hard=bool(status_byte & 0x01),
    )


# =============================================================================
# 0x1A - ATU Segments Tune Parameter (amp -> computer, doc section 3.6.1)
# =============================================================================


@dataclass
class AtuSegmentTuning:
    band_number: int
    low_freq_khz: int
    high_freq_khz: int
    segment_count: int
    current_segment: int
    antenna_number: int
    antenna_type: int
    segment_low_freq_khz: int
    segment_high_freq_khz: int
    tuning_ct_pct: int
    tuning_l_pct: int
    tuning_ca_pct: int
    tuning_age_hours: int


def parse_atu_segment_tuning(frame: Frame) -> AtuSegmentTuning:
    """Parses message 0x1A (22 bytes) - per-band/segment ATU tuning
    quality snapshot. "Generated once on request" per the doc - fetched
    via build_request_atu_tuning_record (0x81 Cmd 0x0F), not streamed."""
    data = frame.data  # bytes 3..20 inclusive (18 bytes)

    def u16(i: int) -> int:
        return data[i] | (data[i + 1] << 8)

    return AtuSegmentTuning(
        band_number=data[0],
        low_freq_khz=u16(1),
        high_freq_khz=u16(3),
        segment_count=data[5],
        current_segment=data[6],
        antenna_number=antenna_from_wire(data[7]),
        antenna_type=data[8],
        segment_low_freq_khz=u16(9),
        segment_high_freq_khz=u16(11),
        tuning_ct_pct=data[13],
        tuning_l_pct=data[14],
        tuning_ca_pct=data[15],
        tuning_age_hours=u16(16),
    )


# =============================================================================
# 0x2A - "ATU Service" menu parameters (amp -> computer, doc section 3.7.8)
# =============================================================================


@dataclass
class AtuServiceInfo:
    model_code: int
    hw_version: int
    hw_subversion: int
    fw_version: int
    fw_subversion: int
    working_hours: int
    tune_ct_pct: int
    tune_l_pct: int
    tune_ca_pct: int
    serial_number: str


def parse_atu_service_info(frame: Frame) -> AtuServiceInfo:
    """Parses message 0x2A (24 bytes) - ATU Service menu parameters: model/
    HW/FW versions, working hours, last-tune quality %, serial number.
    Overlaps 0x16 (identity) but adds working hours + tune%."""
    data = frame.data  # bytes 3..22 inclusive (20 bytes)

    def u16(i: int) -> int:
        return data[i] | (data[i + 1] << 8)

    serial = data[10:20].decode("ascii", errors="replace").rstrip("\x00")
    return AtuServiceInfo(
        model_code=data[0],
        hw_version=data[1],
        hw_subversion=data[2],
        fw_version=data[3],
        fw_subversion=data[4],
        working_hours=u16(5),
        tune_ct_pct=data[7],
        tune_l_pct=data[8],
        tune_ca_pct=data[9],
        serial_number=serial,
    )


# =============================================================================
# 0x2E - "ATU Measure" menu telemetry (amp -> computer, doc section 3.7.9)
# =============================================================================


@dataclass
class AtuMeasureTelemetry:
    temp1_c: float
    temp2_c: float
    input_power_w: float
    forward_power_w: int
    reflected_power_w: int
    atu_ps_mv: int
    antenna_voltage_v: int
    antenna_current_a: float
    antenna_fwd_power_w: int
    antenna_refl_power_w: int
    atu_swr: float
    atu_temp_c: float
    atu_working_hours: int
    sys_status: int


def parse_atu_measure_telemetry(frame: Frame) -> AtuMeasureTelemetry:
    """Parses message 0x2E (31 bytes) - sent by the amp *instead of* 0x2F
    whenever its own front panel is showing the "ATU Measure" menu (doc
    section 3.7.9). Field set differs from 0x2F - kept as a separate
    AmpState slot rather than merged into AmpTelemetry, so the dashboard
    can show real numbers (clearly labeled) instead of frozen 0x2F values
    during that window."""
    data = frame.data  # bytes 3..29 inclusive (27 bytes)

    def u16(i: int) -> int:
        return data[i] | (data[i + 1] << 8)

    return AtuMeasureTelemetry(
        temp1_c=_k_to_c(u16(0)),
        temp2_c=_k_to_c(u16(2)),
        input_power_w=u16(4) / 10,
        forward_power_w=u16(6),
        reflected_power_w=u16(8),
        atu_ps_mv=u16(10),
        antenna_voltage_v=u16(12),
        antenna_current_a=u16(14) / 10,
        antenna_fwd_power_w=u16(16),
        antenna_refl_power_w=u16(18),
        atu_swr=u16(20) / 100,
        atu_temp_c=_k_to_c(u16(22)),
        atu_working_hours=u16(24),
        sys_status=data[26],
    )


# =============================================================================
# 0x31-0x34 - Hard Fault event record, 4 messages (amp -> computer, doc
# section 4) - see build_request_hard_fault_record()'s docstring for the
# "one event, four messages" interpretation this assumes. Every message
# shares the same two leading fields (lifetime count + this record's
# EEPROM slot number) ahead of its own category of data.
# =============================================================================


@dataclass
class HardFaultRecord1:
    """Record 1 (0x31): error-code snapshot + amp mode at the fault."""
    hf_count: int
    hf_num: int
    error_flags: ErrorFlags  # 10 words (Error Codes 0-9) - one fewer than 0x21's 11
    amplifier_mode: int
    amplifier_transition_state: int
    system_counter: int


def parse_hard_fault_record_1(frame: Frame) -> HardFaultRecord1:
    data = frame.data  # bytes 3..36 inclusive (34 bytes)

    def u16(i: int) -> int:
        return data[i] | (data[i + 1] << 8)

    words = [u16(2 + 2 * i) for i in range(10)]  # Error Codes 0-9, Byte5..24
    high_word = u16(28)  # Byte31/32
    low_word = u16(30)  # Byte33/34
    return HardFaultRecord1(
        hf_count=data[0],
        hf_num=data[1],
        error_flags=ErrorFlags(words=words),
        amplifier_mode=u16(24),
        amplifier_transition_state=u16(26),
        system_counter=(high_word << 16) | low_word,
    )


@dataclass
class HardFaultRecord2:
    """Record 2 (0x32): telemetry snapshot at the fault (same shape of
    data as the normal 0x2F cyclic telemetry, frozen at the fault
    moment) - HV2/ID2/PAM2 temp/P_DC2 fields are PAM2-only, "Not Used"
    per the doc on amps without a second PAM."""
    hf_count: int
    hf_num: int
    carrier_freq_khz: int
    forward_power_w: int
    reflected_power_w: int
    input_power_w: float
    vcc26_v: float
    vcc5_mv: int
    hv1_v: float
    hv2_v: float
    id1_ma: int
    id2_ma: int
    pam1_temp_k: int
    pam2_temp_k: int
    pdc1_w: float
    pdc2_w: float


def parse_hard_fault_record_2(frame: Frame) -> HardFaultRecord2:
    data = frame.data  # bytes 3..36 inclusive (34 bytes)

    def u16(i: int) -> int:
        return data[i] | (data[i + 1] << 8)

    return HardFaultRecord2(
        hf_count=data[0],
        hf_num=data[1],
        carrier_freq_khz=u16(2),
        forward_power_w=u16(4),
        reflected_power_w=u16(6),
        input_power_w=u16(8) / 10,
        vcc26_v=u16(12) / 10,
        vcc5_mv=u16(14),
        hv1_v=u16(16) / 10,
        hv2_v=u16(18) / 10,
        id1_ma=u16(20),
        id2_ma=u16(22),
        pam1_temp_k=u16(24),
        pam2_temp_k=u16(26),
        pdc1_w=u16(28) / 10,
        pdc2_w=u16(30) / 10,
    )


@dataclass
class HardFaultRecord3:
    """Record 3 (0x33): bias/dissipation/SWR/CAT-settings snapshot at
    the fault. bias_1[c,d]/bias_2[a-d]/p_dis2 are "Not Used" on amps
    without a second PAM, per the doc."""
    hf_count: int
    hf_num: int
    bias_1a_mv: int
    bias_1b_mv: int
    bias_1c_mv: int
    bias_1d_mv: int
    bias_2a_mv: int
    bias_2b_mv: int
    bias_2c_mv: int
    bias_2d_mv: int
    p_dis1_w: float
    p_dis2_w: float
    swr: float
    analog_band_data_mv: int
    cat_interface_type: int
    cat_command_set: int
    cat_baud_rate: int
    cat_byte_spacing_us: int
    cat_polling_time_ms: int


def parse_hard_fault_record_3(frame: Frame) -> HardFaultRecord3:
    data = frame.data  # bytes 3..36 inclusive (34 bytes)

    def u16(i: int) -> int:
        return data[i] | (data[i + 1] << 8)

    cat1_low, cat1_high = data[26], data[27]
    return HardFaultRecord3(
        hf_count=data[0],
        hf_num=data[1],
        bias_1a_mv=u16(2),
        bias_1b_mv=u16(4),
        bias_1c_mv=u16(6),
        bias_1d_mv=u16(8),
        bias_2a_mv=u16(10),
        bias_2b_mv=u16(12),
        bias_2c_mv=u16(14),
        bias_2d_mv=u16(16),
        p_dis1_w=u16(18) / 10,
        p_dis2_w=u16(20) / 10,
        swr=u16(22) / 100,
        analog_band_data_mv=u16(24),
        cat_interface_type=(cat1_high >> 4) & 0xF,
        cat_command_set=cat1_high & 0xF,
        cat_baud_rate=(cat1_low >> 4) & 0xF,
        cat_byte_spacing_us=u16(28),
        cat_polling_time_ms=u16(30),
    )


@dataclass
class HardFaultRecord4:
    """Record 4 (0x34): bias setpoints (all "Not Used" per the doc -
    kept for completeness, same "every single bit of data" goal as the
    rest of this record), error source, ATU/user-flags/LPF/amplifier-
    flags state at the fault."""
    hf_count: int
    hf_num: int
    bias_1a_set_mv: int
    bias_1b_set_mv: int
    bias_1c_set_mv: int
    bias_1d_set_mv: int
    bias_2a_set_mv: int
    bias_2b_set_mv: int
    bias_2c_set_mv: int
    bias_2d_set_mv: int
    error_source: int
    atu_settings: int
    user_flags: int
    lpf_state: int
    amplifier_flags_1: int
    amplifier_flags_2: int


def parse_hard_fault_record_4(frame: Frame) -> HardFaultRecord4:
    data = frame.data  # bytes 3..36 inclusive (34 bytes)

    def u16(i: int) -> int:
        return data[i] | (data[i + 1] << 8)

    return HardFaultRecord4(
        hf_count=data[0],
        hf_num=data[1],
        bias_1a_set_mv=u16(2),
        bias_1b_set_mv=u16(4),
        bias_1c_set_mv=u16(6),
        bias_1d_set_mv=u16(8),
        bias_2a_set_mv=u16(10),
        bias_2b_set_mv=u16(12),
        bias_2c_set_mv=u16(14),
        bias_2d_set_mv=u16(16),
        error_source=u16(18),
        atu_settings=u16(22),
        user_flags=data[24] | (data[25] << 8),
        lpf_state=u16(26),
        amplifier_flags_1=u16(28),
        amplifier_flags_2=u16(30),
    )
