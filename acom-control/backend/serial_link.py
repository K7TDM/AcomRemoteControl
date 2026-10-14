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
from typing import Callable, Optional

import serial

from . import protocol

logger = logging.getLogger("acom.backend.serial")

INTER_MESSAGE_PACING_S = 0.010  # ~10ms pause required between messages
MAX_REOPEN_RETRIES = 5
READ_CHUNK_SIZE = 256
READ_POLL_INTERVAL_S = 0.02

# DTR/RTS "Remote Power On" pulse: confirmed on real hardware that a 1.5s
# pulse on both lines, then release, toggles the amp's power state (on<->
# off) - deliberately NOT wired into this class, see _open_port().
# send_command() confirmation-wait tuning - see module docstring.
COMMAND_CONFIRM_TIMEOUT_S = 0.3
COMMAND_MAX_ATTEMPTS = 5

# Messages pulled once at startup to populate ATU identity + antenna config
# before the Setup/Antenna Setup screens trust anything - see
# PI_CONTROL_PLAN.md "Setup Wizard & Antenna Setup". 0x2A (ATU Service menu
# params) is requested the same generic way (0x81 Cmd 0x01, Byte4/5 =
# message number) as 0x16/0x18/0x1B - NEEDS REAL-HARDWARE VERIFICATION
# that 0x2A actually responds to this generic request mechanism rather
# than requiring the amp to be in a specific menu/mode first; harmless
# no-op either way if it doesn't.
STARTUP_REQUEST_MESSAGES = (0x16, 0x18, 0x1B, 0x2A)


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

    async def enqueue(self, frame: bytes) -> None:
        await self._out_queue.put(frame)

    async def resync(self) -> None:
        """Re-runs the startup handshake (enable telemetry, pull ATU/
        antenna config) over the already-open port - no close/reopen, so
        this never touches DTR/RTS. Useful when the handshake fired before
        the amp's CPU was ready (e.g. right after a DTR/RTS power-on pulse)
        and telemetry never actually got enabled."""
        await self._startup_handshake()

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
        self._serial = serial.Serial(
            port=self.device,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0,  # non-blocking reads; polled from the asyncio loop below
        )
        # DO NOT touch self._serial.dtr/.rts here. DB9 pins 6 (DSR) / 8
        # (CTS) are wired to the amp's "Remote Power On" input (CLAUDE.md
        # open question 4), and real-hardware testing confirmed a DTR/RTS
        # pulse TOGGLES the amp's power state rather than only turning it
        # on - so pulsing these on every port-open (every backend start,
        # restart, or crash recovery) flips the amp on/off unpredictably
        # depending on whatever state it already happened to be in. Power
        # toggling via these lines must only ever happen from an explicit,
        # deliberate action - never as a side effect of opening the port
        # for normal communication.

    async def _startup_handshake(self) -> None:
        logger.info("Running startup handshake (force HEX mode, enable telemetry, pull ATU/antenna config)")
        await self.enqueue(protocol.build_force_hex_mode())
        await asyncio.sleep(INTER_MESSAGE_PACING_S)
        await self.enqueue(protocol.build_enable_auto_telemetry())
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
                    if self._serial is not None:
                        self._serial.close()
                    self._open_port()
                    reopen_attempts = 0
                    logger.info("Reopened %s - re-running startup handshake", self.device)
                    await self._startup_handshake()
                except Exception as reopen_exc:
                    logger.warning("Failed to reopen %s: %s", self.device, reopen_exc)

    async def _comms_watchdog(self) -> None:
        """Flags comms as lost if nothing's been received for a while -
        telemetry (0x2E/0x2F) should stream continuously once enabled, so a
        long silence means the link (or the amp) is down."""
        comms_ok = False
        while True:
            await asyncio.sleep(1.0)
            now = asyncio.get_running_loop().time()
            healthy = (now - self._last_rx_monotonic) < 10.0 if self._last_rx_monotonic else False
            if healthy != comms_ok:
                comms_ok = healthy
                if self._on_comms_change is not None:
                    self._on_comms_change(comms_ok)

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
