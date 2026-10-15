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


def build_atu_bypass(antenna_number: int) -> bytes:
    """0x0E/0x02 "Bypass Command (Number of Antenna)" - switches directly to
    the given antenna but bypasses ATU tuning. This is the closest thing to
    a direct antenna-select command in this protocol version; see
    CLAUDE.md open question 3.
    """
    return build_amp_command(CMD_ATU_MANAGEMENT, param_hw=antenna_number, param_lw=ATU_BYPASS)


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


def build_user_flags(flags: int) -> bytes:
    return build_amp_command(CMD_USER_FLAGS, param_hw=(flags >> 8) & 0xFF, param_lw=flags & 0xFF)


def _pack_antenna_band(antenna_number: int, band_number: int) -> int:
    """Byte4 bit-packing shared by Cmd 0x0F and Cmd 0x12: Bits7..4 = Antenna
    Number [1..10], Bits3..0 = Band Number [1..11]."""
    return ((antenna_number & 0x0F) << 4) | (band_number & 0x0F)


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
    all tunings, or the whole ATU memory). Byte5 (antenna_number) is only
    meaningful for ATU_ERASE_SPECIFIC_ANTENNA. Destructive and rarely used
    by design - gate behind explicit confirmation."""
    return build_amp_command(CMD_ATU_SERVICE, param_hw=selection, param_lw=antenna_number)


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
        antenna_number=data[0],
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


# Partial lookup for the most severe/common codes - the full ~90-entry
# table is in the protocol doc section 3.7.1; codes not listed here still
# show up as a raw hex code to the frontend.
FAULT_CODE_NAMES: dict[int, str] = {
    0x00: "HOT SWITCHING ATTEMPT",
    0x03: "DRIVE POWER DETECTED AT WRONG TIME",
    0x05: "EXCESSIVE REFLECTED POWER",
    0x07: "EXCESSIVE DRIVE POWER",
    0x0D: "PA LOAD SWR TOO HIGH",
    0x10: "5V TOO LOW",
    0x11: "5V TOO HIGH",
    0x12: "26V TOO LOW",
    0x13: "26V TOO HIGH",
    0x15: "PAM1 FAN SPEED TOO LOW",
    0x16: "PAM2 FAN SPEED TOO LOW",
    0x17: "LPF FAN SPEED TOO LOW",
    0x1E: "PAM1 EXCESSIVE TEMPERATURE",
    0x1F: "PAM2 EXCESSIVE TEMPERATURE",
    0x20: "PAM1 HV TOO LOW",
    0x21: "PAM1 HV TOO HIGH",
    0x40: "PAM2 HV TOO LOW",
    0x41: "PAM2 HV TOO HIGH",
    0x60: "PSU1/CONTROL MALFUNCTION",
    0x61: "PSU2/CONTROL MALFUNCTION",
    0x64: "DISPLAY UNIT COMMUNICATION ERROR",
    0x66: "ATU POWER SWITCH ALARM",
    0x67: "ATU POWER SWITCH ALARM AT POWER ON",
    0x70: "CAT ERROR",
    0x80: "ATU NOT RESPONDING",
    0x81: "ATU-AMP COMMUNICATION ERROR",
    0x82: "AMP-ATU COMMUNICATION ERROR",
    0x83: "ASEL NOT RESPONDING",
    0x84: "ASEL-AMP COMMUNICATION ERROR",
    0x85: "AMP-ASEL COMMUNICATION ERROR",
    0x86: "NO TUNING SETTINGS PREPARED",
    0x87: "NO ANTENNA SETTINGS PREPARED",
    0x8A: "ATU TUNING CYCLE UNSUCCESSFUL",
    0x8B: "ATU MEMORY FAIL",
    0xA8: "ANTENNA REFLECTED POWER TOO HIGH: ATU/ASEL DAMAGE RISK",
    0xA9: "ANTENNA REFLECTED POWER TOO HIGH: ATU/ASEL IS OFF DUE TO DAMAGE RISK",
    0xAC: "ANTENNA SWR TOO HIGH: ATU/ASEL DAMAGE RISK (SOFT FAULT)",
    0xAD: "ANTENNA SWR TOO HIGH: ATU/ASEL DAMAGE RISK (WARNING)",
}


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
        antenna_number=data[7],
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
