"""pyserial transport: framing, startup handshake, ACK-on-receipt, outbound
pacing/retry. Owns the serial port; callers (app.py REST handlers,
modbus_server.py) call `enqueue()` for fire-and-forget sends (status
queries like build_request_message, and 0x86/0x90/0x91/0x92 which get no
OK/ERR reply per the doc) or `send_command()` for anything that should be
confirmed/retried against the amp's 0x03 (OK) / 0x04 (ERR) reply - they
never touch the port directly, so pacing/ACK/retry rules apply uniformly
regardless of caller.

NEEDS REAL-HARDWARE VERIFICATION: the retry/backoff timing, the
confirmation-wait timeout in send_command(), the "ACK by message address"
assumption (see protocol.build_ack), and the startup handshake's
forced-HEX-mode step (0x90 is marked obsolete in the doc) are all
best-effort interpretations of an incompletely-specified retry model. See
PI_CONTROL_PLAN.md "Verification" section.
"""
from __future__ import annotations

import asyncio
import logging
import os
import termios
from typing import Callable, Optional

import serial
import serial.tools.list_ports

from . import protocol

logger = logging.getLogger("acom.backend.serial")

INTER_MESSAGE_PACING_S = 0.010  # ~10ms pause required between messages
MAX_REOPEN_RETRIES = 5
READ_CHUNK_SIZE = 256
READ_POLL_INTERVAL_S = 0.02

# How long without a received byte before comms are considered down.
# Telemetry (0x2E/0x2F) streams continuously once enabled, so this only
# needs to be comfortably longer than one normal gap between frames, not
# a generous cushion - was 10.0s (CLAUDE.md open question 7's TODO to
# tighten this), which combined with app.py's own ASSUME_OFF_AFTER_S
# grace period made a front-panel power-off take up to ~20s to show up
# in the UI. Lowered together with that constant per Tyler's direction.
COMMS_HEALTHY_WINDOW_S = 3.0

# DTR/RTS "Remote Power On" pulse: confirmed on real hardware that a 1.5s
# pulse on both lines, then release, toggles the amp's power state (on<->
# off) - see pulse_power_toggle(). _open_port() deliberately never drives
# these lines itself (see its own comment) - only a deliberate, explicit
# call to pulse_power_toggle() should ever do so.
POWER_PULSE_SECONDS = 1.5

# send_command() confirmation-wait tuning - see module docstring.
COMMAND_CONFIRM_TIMEOUT_S = 0.3
COMMAND_MAX_ATTEMPTS = 5

# Messages pulled once at startup to populate amp/ATU identity + antenna
# config before the Setup/Antenna Setup screens trust anything - see
# PI_CONTROL_PLAN.md "Setup Wizard & Antenna Setup". 0x11 (PCB versions,
# including the amp-model byte used for Setup-screen auto-detection),
# 0x12 ("Settings of the Amplifier" - CAT interface config for the
# Setup screen's CAT/Rig Control section), and 0x2A (ATU Service menu
# params) are requested the same generic way (0x81 Cmd 0x01, Byte4/5 =
# message number) as 0x16/0x18/0x1B - NEEDS REAL-HARDWARE VERIFICATION
# that 0x11/0x12/0x2A actually respond to this generic request
# mechanism rather than requiring the amp to be in a specific menu/mode
# first; harmless no-op either way if they don't.
STARTUP_REQUEST_MESSAGES = (0x11, 0x12, 0x16, 0x18, 0x1B, 0x2A)

# Stable udev symlink from setup/99-acom-amp.rules - always surfaced in
# list_serial_ports() alongside raw enumerated devices, even though pyserial
# itself won't discover it as a distinct comport (see README.md on why the
# symlink exists: raw /dev/ttyUSBx paths renumber across reboots).
STABLE_SYMLINK_DEVICE = "/dev/acom-amp"


def list_serial_ports() -> list[dict]:
    """Enumerates raw USB-serial devices via pyserial, plus the stable udev
    symlink if present, for the Setup screen's serial-port selector."""
    ports = [{"device": p.device, "description": p.description or ""} for p in serial.tools.list_ports.comports()]
    if os.path.exists(STABLE_SYMLINK_DEVICE) and not any(p["device"] == STABLE_SYMLINK_DEVICE for p in ports):
        ports.append({"device": STABLE_SYMLINK_DEVICE, "description": "stable udev symlink"})
    return ports


class SerialLink:
    def __init__(
        self,
        device: str,
        baudrate: int = 9600,
        on_frame: Optional[Callable[[protocol.Frame], None]] = None,
        on_comms_change: Optional[Callable[[bool], None]] = None,
    ) -> None:
        self.device = device
        self.baudrate = baudrate
        self._on_frame = on_frame
        self._on_comms_change = on_comms_change
        self._serial: Optional[serial.Serial] = None
        self._rx_buf = bytearray()
        self._out_queue: "asyncio.Queue[bytes]" = asyncio.Queue()
        self._reader_task: Optional[asyncio.Task] = None
        self._writer_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._last_rx_monotonic: float = 0.0
        self._last_handshake_monotonic: float = 0.0
        # send_command() state - only one confirmed command may be in
        # flight at a time, matching the protocol's own single-outstanding-
        # message retry model (see protocol.build_ack's docstring).
        self._confirmation_lock = asyncio.Lock()
        self._pending_confirmation: "Optional[asyncio.Future[protocol.CommandAck]]" = None

    async def start(self) -> None:
        self._open_port()
        self._reader_task = asyncio.create_task(self._read_loop(), name="serial-reader")
        self._writer_task = asyncio.create_task(self._write_loop(), name="serial-writer")
        self._watchdog_task = asyncio.create_task(self._comms_watchdog(), name="serial-watchdog")
        await self._startup_handshake()

    async def stop(self) -> None:
        for task in (self._reader_task, self._writer_task, self._watchdog_task):
            if task is not None:
                task.cancel()
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass

    async def reconfigure(self, new_device: str) -> None:
        """Live port switch (Setup screen serial-port selector) - stops the
        reader/writer/watchdog tasks, closes the current port, points at
        new_device, reopens, and redoes the startup handshake. Raises on
        failure (bad/nonexistent port) rather than leaving the link
        half-torn-down; the caller (app.py) decides what to tell the user
        and must not persist new_device if this raises."""
        for task in (self._reader_task, self._writer_task, self._watchdog_task):
            if task is not None:
                task.cancel()
        self._reader_task = self._writer_task = self._watchdog_task = None
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

        self.device = new_device
        self._open_port()  # let failures propagate - caller must not persist new_device

        # Reset watchdog state so comms doesn't briefly read as healthy from
        # stale rx data captured on the old port.
        self._last_rx_monotonic = 0.0
        self._reader_task = asyncio.create_task(self._read_loop(), name="serial-reader")
        self._writer_task = asyncio.create_task(self._write_loop(), name="serial-writer")
        self._watchdog_task = asyncio.create_task(self._comms_watchdog(), name="serial-watchdog")
        await self._startup_handshake()

    async def enqueue(self, frame: bytes) -> None:
        await self._out_queue.put(frame)

    async def resync(self) -> None:
        """Re-runs the startup handshake (enable telemetry, pull ATU/
        antenna config) over the already-open port - no close/reopen, so
        this never touches DTR/RTS. Useful when the handshake fired before
        the amp's CPU was ready (e.g. right after a DTR/RTS power-on pulse)
        and telemetry never actually got enabled."""
        await self._startup_handshake()

    async def pulse_power_toggle(self) -> None:
        """Asserts DTR+RTS together for ~1.5s then releases - confirmed on
        real 1200S hardware (CLAUDE.md open question 4/5) to cleanly
        toggle the amp's power state in either direction, 4/4 clean on the
        day it was characterized (2x recovering an already-hung amp, 2x as
        a direct power-off from normal operation) - unlike the `0x81`/
        `0x02`/`0x0A` software "turn off" command, which hangs the amp's
        own internal shutdown sequence every time it's been tried.

        This is a BLIND TOGGLE, not a deterministic on/off: pulsing while
        the amp is already off would turn it back on. Callers MUST only
        invoke this when the current power state is actually known (see
        app.py's set_power(), which refuses unless comms_ok) - this method
        itself has no way to check that.

        Reuses the already-open connection rather than a separate fd, so
        _read_loop()/_write_loop() keep running on the same port
        throughout - DTR/RTS are independent of the TX/RX data lines, so
        toggling them here doesn't interfere with reads/writes in flight.
        """
        assert self._serial is not None
        logger.info("Pulsing DTR+RTS for %.1fs to toggle amp power", POWER_PULSE_SECONDS)
        self._serial.dtr = True
        self._serial.rts = True
        await asyncio.sleep(POWER_PULSE_SECONDS)
        self._serial.dtr = False
        self._serial.rts = False
        logger.info("Released DTR+RTS after %.1fs pulse", POWER_PULSE_SECONDS)

    async def send_command(
        self,
        frame: bytes,
        max_attempts: int = COMMAND_MAX_ATTEMPTS,
        timeout_s: float = COMMAND_CONFIRM_TIMEOUT_S,
    ) -> protocol.CommandAck:
        """Writes `frame` and waits for the amp's 0x03 (OK) / 0x04 (ERR) -
        or 0x01 malformed-reply - response, resending up to `max_attempts`
        times on timeout before giving up (doc section 1: unconfirmed
        messages are resent, discarded after the fifth attempt).

        Writes directly (bypassing the pacing queue) rather than going
        through `enqueue()`, so the confirmation-wait window isn't skewed
        by whatever else happens to be queued ahead of it; `_confirmation_lock`
        ensures only one send_command() runs at a time, so this never races
        the write loop's own pacing in a way that reorders frames relative
        to other send_command() calls. Fire-and-forget frames sent via
        `enqueue()` can still interleave on the wire between attempts -
        that's fine, they don't expect a reply.
        """
        async with self._confirmation_lock:
            for attempt in range(1, max_attempts + 1):
                loop = asyncio.get_running_loop()
                fut: "asyncio.Future[protocol.CommandAck]" = loop.create_future()
                self._pending_confirmation = fut
                try:
                    assert self._serial is not None
                    self._serial.write(frame)
                    logger.debug("TX (confirmed, attempt %d/%d) %s", attempt, max_attempts, frame.hex(" "))
                except Exception:
                    logger.exception("Failed writing confirmed frame to serial port")
                    self._pending_confirmation = None
                    await asyncio.sleep(INTER_MESSAGE_PACING_S)
                    continue
                try:
                    result = await asyncio.wait_for(fut, timeout_s)
                    return result
                except asyncio.TimeoutError:
                    logger.warning(
                        "No OK/ERR reply within %.2fs (attempt %d/%d): %s",
                        timeout_s, attempt, max_attempts, frame.hex(" "),
                    )
                finally:
                    if self._pending_confirmation is fut:
                        self._pending_confirmation = None
                    await asyncio.sleep(INTER_MESSAGE_PACING_S)
            logger.error("Command not confirmed after %d attempts, giving up: %s", max_attempts, frame.hex(" "))
            return protocol.CommandAck(ok=False, param=0)

    def _open_port(self) -> None:
        logger.info("Opening serial port %s @ %d 8N1", self.device, self.baudrate)
        # DB9 pins 6 (DSR) / 8 (CTS) are wired to the amp's "Remote Power
        # On" input (CLAUDE.md open question 4). Driving DTR/RTS HIGH on
        # open (pyserial's default) hung the amp on a real port reopen;
        # driving them explicitly LOW instead (commit 2ec2387) was tried
        # next and *also* hung it on power-down, and separately, plugging
        # the cable back in while off still powered the amp on even with
        # that fix in place - real-hardware testing showed any explicit
        # level change on these lines, not just a HIGH pulse, can read as
        # a toggle. So this now avoids driving DTR/RTS at all, in either
        # direction, on open OR close - leaving them in whatever idle
        # state the USB-serial chip already sits in before we touch the
        # fd, per Tyler's direction after that test.
        self._serial = serial.Serial()
        self._serial.port = self.device
        self._serial.baudrate = self.baudrate
        self._serial.bytesize = serial.EIGHTBITS
        self._serial.parity = serial.PARITY_NONE
        self._serial.stopbits = serial.STOPBITS_ONE
        self._serial.timeout = 0  # non-blocking reads; polled from the asyncio loop below
        # Neuter *only* pyserial's own open()-time DTR/RTS ioctls, directly
        # rather than via the dsrdtr/rtscts constructor flags (rtscts=True
        # would also flip on termios hardware RTS/CTS flow control, which
        # the amp doesn't use for UART flow control and could stall writes
        # waiting for a CTS transition that never comes the way flow
        # control expects). Restored right after open() - a deliberate
        # pulse (pulse_power_toggle()) still needs the real methods so a
        # later .dtr/.rts assignment actually reaches the hardware.
        real_update_dtr = self._serial._update_dtr_state
        real_update_rts = self._serial._update_rts_state
        self._serial._update_dtr_state = lambda: None
        self._serial._update_rts_state = lambda: None
        self._serial.open()
        self._serial._update_dtr_state = real_update_dtr
        self._serial._update_rts_state = real_update_rts
        # pyserial doesn't touch HUPCL, so whatever the port's termios
        # already had persists - clear it explicitly so close() doesn't
        # have the kernel drop DTR as a hangup side effect either.
        try:
            attrs = termios.tcgetattr(self._serial.fd)
            attrs[2] &= ~termios.HUPCL  # cflag is index 2
            termios.tcsetattr(self._serial.fd, termios.TCSANOW, attrs)
        except (termios.error, AttributeError):
            logger.warning("Could not clear HUPCL on %s - close() may still drop DTR/RTS", self.device)

    async def _startup_handshake(self) -> None:
        logger.info("Running startup handshake (force HEX mode, enable telemetry, pull ATU/antenna config)")
        self._last_handshake_monotonic = asyncio.get_running_loop().time()
        await self.enqueue(protocol.build_force_hex_mode())
        await asyncio.sleep(INTER_MESSAGE_PACING_S)
        await self.enqueue(protocol.build_enable_auto_telemetry())
        await asyncio.sleep(INTER_MESSAGE_PACING_S)
        # Documented (doc section 5.1's ATU Management table / CLAUDE.md's
        # protocol facts) but never actually sent until now - distinct
        # from build_enable_auto_telemetry()'s blanket "enable automatic
        # telemetry", which only confirmed-covers 0x2F. Candidate unlock
        # for CLAUDE.md open question 6 (0x18/0x1B/0x2A staying silent) -
        # see build_atu_activate_cyclic_display()'s docstring. Harmless if
        # it turns out unrelated: these two values only ever mean "turn on
        # this specific cyclic display message," nothing else.
        await self.enqueue(protocol.build_atu_activate_cyclic_display(protocol.ATU_ACTIVATE_CYCLIC_2E))
        await asyncio.sleep(INTER_MESSAGE_PACING_S)
        await self.enqueue(protocol.build_atu_activate_cyclic_display(protocol.ATU_ACTIVATE_CYCLIC_2F))
        await asyncio.sleep(INTER_MESSAGE_PACING_S)
        for message_address in STARTUP_REQUEST_MESSAGES:
            await self.enqueue(protocol.build_request_message(message_address))
            await asyncio.sleep(INTER_MESSAGE_PACING_S)

    async def _write_loop(self) -> None:
        while True:
            frame = await self._out_queue.get()
            try:
                assert self._serial is not None
                self._serial.write(frame)
                logger.debug("TX %s", frame.hex(" "))
            except Exception:
                logger.exception("Failed writing frame to serial port")
            await asyncio.sleep(INTER_MESSAGE_PACING_S)

    async def _read_loop(self) -> None:
        reopen_attempts = 0
        while True:
            try:
                assert self._serial is not None
                chunk = self._serial.read(READ_CHUNK_SIZE)
                if chunk:
                    self._rx_buf += chunk
                    self._last_rx_monotonic = asyncio.get_running_loop().time()
                    self._drain_buffer()
                    reopen_attempts = 0
                else:
                    await asyncio.sleep(READ_POLL_INTERVAL_S)
            except (OSError, serial.SerialException) as exc:
                reopen_attempts += 1
                logger.warning(
                    "Serial read error (%s) on %s - reopen attempt %d", exc, self.device, reopen_attempts
                )
                backoff = min(1.0 * reopen_attempts, 5.0)
                await asyncio.sleep(backoff)
                try:
                    self._reopen_port()
                    reopen_attempts = 0
                    logger.info("Reopened %s - re-running startup handshake", self.device)
                    await self._startup_handshake()
                except Exception as reopen_exc:
                    logger.warning("Failed to reopen %s: %s", self.device, reopen_exc)

    def _reopen_port(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
        self._open_port()

    async def _comms_watchdog(self) -> None:
        """Flags comms as lost if nothing's been received for a while -
        telemetry (0x2E/0x2F) should stream continuously once enabled, so a
        long silence means the link (or the amp) is down.

        While unhealthy, also gently re-sends the startup handshake every
        few seconds (debounced, not every tick). The amp forgets "auto
        telemetry enabled" across its own power cycle (e.g. the DTR/RTS
        power-cycle recovery for the item-5 off-hang in CLAUDE.md), and in
        that case the fd never errors/closes, so _read_loop()'s own
        reopen-triggered handshake never fires - and since the amp sends
        nothing on its own until re-prompted, waiting for a frame to
        arrive before re-enabling telemetry is a chicken-and-egg problem
        (confirmed on real hardware: comms stayed down until a manual
        POST /api/resync). Periodically re-sending while down means
        whichever attempt lands after the amp's CPU finishes booting is
        the one that gets through, instead of requiring a manual poke."""
        comms_ok = False
        while True:
            await asyncio.sleep(1.0)
            now = asyncio.get_running_loop().time()
            healthy = (now - self._last_rx_monotonic) < COMMS_HEALTHY_WINDOW_S if self._last_rx_monotonic else False
            if healthy != comms_ok:
                comms_ok = healthy
                if self._on_comms_change is not None:
                    self._on_comms_change(comms_ok)
            if not healthy and (now - self._last_handshake_monotonic) > 5.0:
                logger.info("Comms down - re-sending startup handshake in case the amp power-cycled")
                await self._startup_handshake()

    def _drain_buffer(self) -> None:
        buf = bytes(self._rx_buf)
        while buf:
            frame, remaining = protocol.try_parse_frame(buf)
            if frame is None and len(remaining) == len(buf):
                break  # not enough bytes yet for a full frame
            if frame is not None:
                logger.debug("RX %s", frame.raw.hex(" "))
                self._handle_frame(frame)
            buf = remaining
        self._rx_buf = bytearray(buf)

    def _handle_frame(self, frame: protocol.Frame) -> None:
        if frame.address in (0x01, 0x03, 0x04):
            fut = self._pending_confirmation
            if fut is not None and not fut.done():
                if frame.address == 0x03:
                    fut.set_result(protocol.parse_ok(frame))
                elif frame.address == 0x04:
                    fut.set_result(protocol.parse_err(frame))
                else:
                    fut.set_result(protocol.parse_malformed_reply(frame))
        if self._on_frame is not None:
            try:
                self._on_frame(frame)
            except Exception:
                logger.exception("on_frame callback raised for address 0x%02X", frame.address)
        if frame.address != 0x86:
            # Every amp-initiated message must be ACKed by the computer,
            # per doc section 1, or the amp resends it (up to 5 attempts).
            self._out_queue.put_nowait(protocol.build_ack(frame.address))
