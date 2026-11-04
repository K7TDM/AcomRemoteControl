"""FastAPI app: HTTP/WebSocket API, static dashboard, startup/shutdown
wiring. See PI_CONTROL_PLAN.md step 4 for the endpoint list this
implements, and "Backend vs. frontend logging" for the two-logger design.

Run during development with:
    uvicorn backend.app:app --host 0.0.0.0 --port 8000
(from the acom-control/ directory, with the venv's packages installed).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import socket
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import protocol
from .amp_profiles import AMP_PROFILES, AMP_TYPE_CODE_TO_MODEL, TUNER_MODELS, get_amp_profile
from .amp_state import AmpState
from .config import (
    DISPLAY_BRIGHTNESS_MIN,
    DISPLAY_PANEL_DEFAULTS,
    AppConfig,
    load_config,
    normalize_display_brightness,
    normalize_display_panels,
    validate_config,
)
from .modbus_server import ModbusServer
from .serial_link import SerialLink, list_serial_ports

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

backend_logger = logging.getLogger("acom.backend")
frontend_logger = logging.getLogger("acom.frontend")

# Prefix for every git command this (unprivileged) process runs itself.
# The repo is owned by whoever ran install.sh, but the service runs as
# the acom-control user, and git refuses to work in a repo owned by
# someone else ("detected dubious ownership") - which made every call
# below fail on the real Pi, so GIT_VERSION was stuck at "unknown" and
# the ?v= asset cache-busting never changed between updates (browsers
# needed a hard refresh). safe.directory is honored when passed on the
# command line, so this trusts exactly this one repo, for these calls
# only, without touching any user's or the system's git config.
GIT_CMD = ["git", "-c", f"safe.directory={REPO_ROOT}", "-C", str(REPO_ROOT)]


def _read_git_commit() -> str:
    """Short commit hash this process was started from - computed once at
    import time (cheap: the process restarts after any real update, so a
    stale cached value would only ever happen if something else patched
    files without restarting). Shown in the update popover, and used by
    the frontend to detect when POST /api/update/apply's restart has
    actually landed, rather than guessing from connectivity blips alone."""
    try:
        result = subprocess.run(
            [*GIT_CMD, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        return result.stdout.strip()
    except Exception as exc:
        backend_logger.warning("Could not read git commit: %s", _git_error(exc))
        return "unknown"


def _git_error(exc: Exception) -> str:
    """git's own stderr when there is one (e.g. "dubious ownership"),
    rather than just the exit status."""
    stderr = getattr(exc, "stderr", None)
    return stderr.strip() if stderr else str(exc)


GIT_COMMIT = _read_git_commit()


def _read_git_version() -> str:
    """Human-facing version string - the nearest reachable release tag
    (see CHANGELOG.md), e.g. "v0.1.0" if HEAD is exactly at that tag, or
    "v0.1.0-3-gabc1234" if 3 commits past it (standard `git describe`
    format). Falls back to the raw short hash if no tag exists yet.
    Shown in the update popover instead of GIT_COMMIT's raw hash, which
    stays reserved for the frontend's "did the restart actually land"
    check - that one needs to just change on any new commit, not track
    releases specifically."""
    try:
        result = subprocess.run(
            [*GIT_CMD, "describe", "--tags", "--always"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        return result.stdout.strip()
    except Exception as exc:
        backend_logger.warning("Could not read git version: %s", _git_error(exc))
        return "unknown"


GIT_VERSION = _read_git_version()

TX_ARM_WINDOW_S = 15.0

app = FastAPI(title="ACOM Remote Control")

config: AppConfig = load_config()
amp_state = AmpState(profile=get_amp_profile(config.amp.model), tuner_model=config.tuner.model)

serial_link: Optional[SerialLink] = None
modbus_server: Optional[ModbusServer] = None
_tx_arm_token: Optional[str] = None
_tx_arm_expires: float = 0.0

# Fire-and-forget asyncio.Task bookkeeping: a task with no retained
# reference is GC-eligible mid-flight and swallows its own exceptions
# silently. Every asyncio.create_task() call in this module goes through
# `_spawn()` instead, which keeps a reference until the task finishes and
# logs anything it raised.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_on_background_task_done)
    return task


def _on_background_task_done(task: asyncio.Task) -> None:
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        backend_logger.error("Background task %s failed", task.get_name(), exc_info=exc)


def _configure_logging() -> None:
    level = getattr(logging, config.logging.level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(name)s %(levelname)s: %(message)s")


class ConnectionManager:
    """Tracks connected dashboard WebSocket clients and fans out AmpState
    updates to all of them."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


ws_manager = ConnectionManager()

# Recovery for an amp streaming 0x2E ("ATU Measure" display telemetry)
# instead of 0x2F main telemetry - seen on real hardware after every
# restart while the startup handshake still sent "activate cyclic 0x2E"
# (see serial_link._startup_handshake()). The dashboard looks dead in that
# state even though comms are fine. Asking for 0x2F again is the
# documented switch back (0x0E/0x2F); an STB/OPR mode change is also
# confirmed to restore it. NEEDS REAL-HARDWARE VERIFICATION that the 0x2F
# activation alone is enough.
MAIN_TELEMETRY_STALE_S = 5.0
MAIN_TELEMETRY_RESTORE_INTERVAL_S = 10.0
_last_main_telemetry_monotonic = 0.0
_last_main_telemetry_restore_monotonic = 0.0


def _maybe_restore_main_telemetry() -> None:
    global _last_main_telemetry_restore_monotonic
    if serial_link is None:
        return
    now = time.monotonic()
    if now - _last_main_telemetry_monotonic < MAIN_TELEMETRY_STALE_S:
        return
    if now - _last_main_telemetry_restore_monotonic < MAIN_TELEMETRY_RESTORE_INTERVAL_S:
        return
    _last_main_telemetry_restore_monotonic = now
    backend_logger.info("Amp is streaming ATU Measure (0x2E) instead of main telemetry (0x2F) - asking for 0x2F")
    _spawn(serial_link.enqueue(protocol.build_atu_activate_cyclic_display(protocol.ATU_ACTIVATE_CYCLIC_2F)))


def _on_frame(frame: protocol.Frame) -> None:
    """Dispatches a decoded frame onto AmpState and schedules a WebSocket
    broadcast. Runs synchronously from SerialLink's asyncio read-loop task
    (same event loop, not a separate thread), so asyncio.create_task here
    is safe."""
    global _last_main_telemetry_monotonic
    try:
        handled = True
        if frame.address == 0x2F:
            _last_main_telemetry_monotonic = time.monotonic()
            amp_state.update_telemetry(protocol.parse_telemetry(frame, amp_state.profile))
        elif frame.address == 0x11:
            identity = protocol.parse_amplifier_identity(frame)
            amp_state.update_amplifier_identity(identity)
            _maybe_apply_detected_amp_model(identity)
        elif frame.address == 0x12:
            amp_state.update_amp_settings(protocol.parse_amp_settings(frame))
        elif frame.address == 0x16:
            amp_state.update_atu_identity(protocol.parse_atu_identity(frame))
        elif frame.address == 0x18:
            amp_state.update_antenna_assignments(protocol.parse_antenna_assignments(frame))
        elif frame.address == 0x1B:
            amp_state.update_antenna_names(protocol.parse_antenna_names(frame))
        elif frame.address == 0x27:
            amp_state.update_antenna_band_status(protocol.parse_antenna_band_status(frame))
        elif frame.address == 0x21:
            amp_state.update_error_flags(protocol.parse_error_flags(frame))
        elif frame.address == 0x1A:
            amp_state.update_atu_segment_tuning(protocol.parse_atu_segment_tuning(frame))
        elif frame.address == 0x2A:
            amp_state.update_atu_service_info(protocol.parse_atu_service_info(frame))
        elif frame.address == 0x2E:
            amp_state.update_atu_measure(protocol.parse_atu_measure_telemetry(frame))
            _maybe_restore_main_telemetry()
        elif frame.address == 0x31:
            amp_state.update_hard_fault_record_1(protocol.parse_hard_fault_record_1(frame))
        elif frame.address == 0x32:
            amp_state.update_hard_fault_record_2(protocol.parse_hard_fault_record_2(frame))
        elif frame.address == 0x33:
            amp_state.update_hard_fault_record_3(protocol.parse_hard_fault_record_3(frame))
        elif frame.address == 0x34:
            amp_state.update_hard_fault_record_4(protocol.parse_hard_fault_record_4(frame))
        elif frame.address == 0x03:
            amp_state.update_last_command_result(protocol.parse_ok(frame))
        elif frame.address == 0x04:
            amp_state.update_last_command_result(protocol.parse_err(frame))
            backend_logger.warning("Amplifier returned ERR for last command (param=%s)", frame.data.hex(" "))
        elif frame.address == 0x01:
            amp_state.update_last_command_result(protocol.parse_malformed_reply(frame))
            backend_logger.warning("Amplifier reported our last message was malformed")
        else:
            handled = False
            backend_logger.debug("Unhandled frame address 0x%02X (%d bytes)", frame.address, len(frame.raw))
        if handled:
            _spawn(ws_manager.broadcast(amp_state.to_dict()))
    except Exception:
        backend_logger.exception("Failed to parse frame at address 0x%02X", frame.address)


def _maybe_apply_detected_amp_model(identity: protocol.AmplifierIdentity) -> None:
    """Auto-selects the AmpProfile from the amp's own self-reported type
    (message 0x11) when it maps to a known model and differs from the
    currently configured one - see PI_CONTROL_PLAN.md's amp-model
    auto-detection note. Unrecognized/unmapped codes (including 2000S,
    which this message can't represent - see CLAUDE.md open question 2)
    are a no-op: the manual Setup-screen dropdown keeps governing."""
    detected_model = AMP_TYPE_CODE_TO_MODEL.get(identity.type_code)
    if detected_model is None or detected_model == config.amp.model:
        return
    backend_logger.info(
        "Amp self-reported type 0x%02X (%s) - auto-switching amp model %s -> %s",
        identity.type_code, identity.type_name, config.amp.model, detected_model,
    )
    config.amp.model = detected_model
    amp_state.profile = get_amp_profile(detected_model)
    config.save()


ASSUME_OFF_AFTER_S = 3.0
# After a Power On pulse: how long to wait for the amp to boot and start
# replying before concluding the pulse didn't wake it (unplugged, mains
# off, ...). Longer than ASSUME_OFF_AFTER_S - the amp's CPU needs time to
# come up and the handshake only re-sends every ~5s.
POWER_ON_ASSUME_OFF_AFTER_S = 15.0


def _on_comms_change(ok: bool) -> None:
    if ok:
        amp_state.mark_comms_ok()
    else:
        was_on = amp_state.power_state == "on"
        amp_state.mark_comms_lost()
        backend_logger.warning("Lost comms with amplifier (no frames received recently)")
        if was_on:
            # Comms were just confirmed "on" and then went ambiguous
            # (hung/cable/fault/actually off?) - mark_comms_lost() already
            # degrades this to "unknown" rather than guessing, which is
            # correct, but leaves no way to recover via the UI (both the
            # Power On pulse and the mode-command branches are gated off
            # "unknown"). _comms_watchdog() is already re-sending the
            # handshake every ~5s - give it one more chance (per Tyler's
            # direction) before concluding the amp is actually off.
            _spawn(_assume_off_after_timeout())
    _spawn(ws_manager.broadcast(amp_state.to_dict()))


async def _assume_off_after_timeout(delay_s: float = ASSUME_OFF_AFTER_S, reason: str = "losing comms") -> None:
    await asyncio.sleep(delay_s)
    if amp_state.power_state == "unknown":
        backend_logger.info("No reply within %.0fs of %s - assuming amplifier is off", delay_s, reason)
        amp_state.mark_power_off_confirmed()
        _spawn(ws_manager.broadcast(amp_state.to_dict()))


def _require_serial_link() -> SerialLink:
    if serial_link is None:
        raise HTTPException(503, "serial link not yet started")
    return serial_link


@app.on_event("startup")
async def on_startup() -> None:
    global serial_link, modbus_server, _update_check_task
    _configure_logging()
    backend_logger.info("Starting ACOM Remote Control backend (amp=%s tuner=%s)", config.amp.model, config.tuner.model)
    serial_link = SerialLink(device=config.amp.serial_device, on_frame=_on_frame, on_comms_change=_on_comms_change)
    try:
        await serial_link.start()
    except Exception:
        backend_logger.exception(
            "Failed to open configured serial port %r at startup - backend is up but "
            "comms are down; pick a working port from the Setup screen.",
            config.amp.serial_device,
        )
    # Covers the case _on_comms_change(False) can't: the amp may already
    # be off *before* comms were ever established, so there's no on->off
    # edge to hook the assume-off timeout onto - give a fresh boot the
    # same bounded chance to prove it's actually on before concluding off.
    _spawn(_assume_off_after_timeout())
    if config.modbus.enabled:
        modbus_server = ModbusServer(
            host=config.modbus.host, port=config.modbus.port, amp_state=amp_state, serial_link=serial_link
        )
        await modbus_server.start()
    _update_check_task = _spawn(_update_check_loop())


@app.on_event("shutdown")
async def on_shutdown() -> None:
    if _update_check_task is not None:
        _update_check_task.cancel()
    if modbus_server is not None:
        await modbus_server.stop()
    if serial_link is not None:
        await serial_link.stop()


# =============================================================================
# Status + WebSocket
# =============================================================================


@app.get("/api/status")
async def get_status() -> dict:
    return amp_state.to_dict()


def _local_ipv4_address() -> Optional[str]:
    """The Pi's main LAN address, for the dashboard footer - so someone
    without a static IP can read it off the kiosk screen and open the
    dashboard from another device. One address even with both Ethernet and
    Wi-Fi up: the one the default route uses (the UDP connect sends
    nothing, it just asks the kernel which source address it would pick),
    falling back to the first from `hostname -I` with no default route."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            addr = s.getsockname()[0]
        if not addr.startswith("127."):
            return addr
    except OSError:
        pass
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=2).stdout
        addrs = [a for a in out.split() if "." in a and not a.startswith("127.")]
        return addrs[0] if addrs else None
    except (OSError, subprocess.SubprocessError):
        return None


@app.get("/api/network")
async def get_network(request: Request) -> dict:
    # The port the dashboard was actually reached on (8000 from the kiosk's
    # localhost URL), so the footer shows an address that works as-is.
    return {"address": _local_ipv4_address(), "port": request.url.port}


@app.post("/api/resync")
async def resync() -> dict:
    """Re-sends the enable-telemetry/pull-config handshake over the
    already-open serial port - no port close/reopen, so this never touches
    DTR/RTS (see CLAUDE.md open question 4 and serial_link.SerialLink._open_port).
    Useful if the handshake fired before the amp's CPU was ready, e.g. right
    after a DTR/RTS-triggered power-on, and telemetry never got enabled."""
    link = _require_serial_link()
    await link.resync()
    return {"ok": True}


async def _wait_for_matching_reply(predicate, timeout_s: float, poll_interval_s: float = 0.02):
    """Polls `predicate()` (reads straight from amp_state) until it
    returns something truthy or `timeout_s` elapses. This protocol has
    no request/reply correlation ID, so this is the only way to line a
    reply up with the request that (hopefully) triggered it - safe
    here specifically because every reply this is used for self-
    identifies (ATU tuning records carry their own antenna/band; Hard
    Fault records each land on their own fixed address), so polling
    AmpState for "the thing I just asked for" can't be confused by a
    reply to some unrelated, earlier request."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        result = predicate()
        if result is not None:
            return result
        await asyncio.sleep(poll_interval_s)
    return None


async def _collect_atu_tuning_matrix(link: SerialLink, timeout_s: float = 0.3) -> list[dict]:
    """Loops over every antenna x band combination (4 x 11 = 44),
    requesting each one's stored ATU tuning record (0x81 Cmd 0x0F) and
    waiting briefly for the amp's self-identifying 0x1A reply. Not part
    of the normal startup handshake (too slow/unnecessary for every
    dashboard session) - only run as part of the full amp-snapshot
    download. No reply within the timeout is treated as "no stored
    tuning for this combo," not an error - plausible for antenna/band
    pairs that were never tuned. NEEDS REAL-HARDWARE VERIFICATION -
    never exercised against a real amp."""
    results = []
    for antenna_number in range(1, protocol.NUM_ANTENNAS + 1):
        for band_number in range(1, len(protocol.BAND_NAMES) + 1):
            await link.enqueue(protocol.build_request_atu_tuning_record(antenna_number, band_number))

            def check():
                t = amp_state.atu_segment_tuning
                if t and t.antenna_number == antenna_number and t.band_number == band_number:
                    return t
                return None

            tuning = await _wait_for_matching_reply(check, timeout_s)
            if tuning is not None:
                results.append({
                    "antenna_number": antenna_number,
                    "band_number": band_number,
                    "band_name": protocol.BAND_NAMES[band_number - 1],
                    **_atu_segment_tuning_dict(tuning),
                })
    return results


def _atu_segment_tuning_dict(t: protocol.AtuSegmentTuning) -> dict:
    return {
        "low_freq_khz": t.low_freq_khz,
        "high_freq_khz": t.high_freq_khz,
        "segment_count": t.segment_count,
        "current_segment": t.current_segment,
        "antenna_type": t.antenna_type,
        "segment_low_freq_khz": t.segment_low_freq_khz,
        "segment_high_freq_khz": t.segment_high_freq_khz,
        "tuning_ct_pct": t.tuning_ct_pct,
        "tuning_l_pct": t.tuning_l_pct,
        "tuning_ca_pct": t.tuning_ca_pct,
        "tuning_age_hours": t.tuning_age_hours,
    }


async def _collect_hard_fault_records(link: SerialLink, timeout_s: float = 0.5) -> dict:
    """Requests all four Hard Fault record messages (0x81 Cmd 0x03,
    Byte5=1..4) and waits for each one's own fixed-address reply
    (0x31-0x34) - see build_request_hard_fault_record()'s docstring for
    the "one event, four messages" interpretation this assumes. Not
    part of the normal startup handshake - on-demand only, as part of
    the full amp-snapshot download. NEEDS REAL-HARDWARE VERIFICATION -
    never exercised against a real amp."""
    records: dict[str, Optional[dict]] = {}
    specs = [
        (1, lambda: amp_state.hard_fault_record_1, "record_1"),
        (2, lambda: amp_state.hard_fault_record_2, "record_2"),
        (3, lambda: amp_state.hard_fault_record_3, "record_3"),
        (4, lambda: amp_state.hard_fault_record_4, "record_4"),
    ]
    for record_number, getter, key in specs:
        before = getter()
        await link.enqueue(protocol.build_request_hard_fault_record(record_number))
        after = await _wait_for_matching_reply(lambda: getter() if getter() is not before else None, timeout_s)
        records[key] = asdict(after) if after is not None else None
    return records


@app.get("/api/amp-snapshot")
async def download_amp_snapshot() -> Response:
    """Re-syncs (same as "Refresh from Amp"/POST /api/resync), then
    pulls every piece of amp state this backend knows how to ask for -
    including the full per-antenna/per-band ATU tuning matrix and all
    four Hard Fault record messages, neither of which the normal
    startup handshake requests - into one downloadable JSON file.
    Meant for comparing real-hardware setups over time (different
    radio, tuner vs. no tuner, etc.), not for any single field.
    Re-syncing doesn't make a message reply that genuinely never does
    on this hardware - antenna names/band assignments (0x18/0x1B) are
    included if present, but are expected to still be empty per
    CLAUDE.md open question 6 until that's otherwise resolved; this is
    a capture tool, not a fix for it. Can take 10+ seconds (44 paced
    ATU tuning requests, each with its own short reply-wait) - that's
    expected, not a hang."""
    link = _require_serial_link()
    await link.resync()
    await asyncio.sleep(1.0)  # give the resync's own replies a moment to land first
    atu_tuning_matrix = await _collect_atu_tuning_matrix(link)
    hard_fault_records = await _collect_hard_fault_records(link)
    snapshot = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "backend_version": GIT_VERSION,
        "config": {
            "amp_model": config.amp.model,
            "tuner_model": config.tuner.model,
            "serial_device": config.amp.serial_device,
        },
        "amp_state": amp_state.to_dict(),
        "atu_tuning_matrix": atu_tuning_matrix,
        "hard_fault_records": hard_fault_records,
    }
    filename = f"acom-amp-snapshot-{time.strftime('%Y%m%d-%H%M%S')}.json"
    return Response(
        content=json.dumps(snapshot, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.websocket("/ws/status")
async def ws_status(ws: WebSocket) -> None:
    await ws_manager.connect(ws)
    try:
        await ws.send_json(amp_state.to_dict())
        while True:
            # The dashboard doesn't send anything meaningful over this
            # socket; just block until the client disconnects.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(ws)


# =============================================================================
# Power / mode / band / antenna cycling
# =============================================================================


def _require_confirmed(result: protocol.CommandAck) -> dict:
    if not result.ok:
        raise HTTPException(502, f"amplifier did not confirm command (param=0x{result.param:04X})")
    return {"ok": True, "param": result.param}


async def _send_pulse_command(link: SerialLink, frame: bytes) -> dict:
    """For momentary/pulse-style 0x81 commands (power, mode, band/antenna
    cycle) - confirmed on real 1200S hardware to NOT reply with 0x03/0x04,
    so send_command()'s resend-on-timeout retry just re-executes the same
    physical action each attempt (observed: 5 retries of "Next Band" cycled
    the band 5 times; the same storm on an OPR/TX mode change is a real
    safety issue). Single attempt only, and a missing confirmation is not
    treated as failure - the resulting telemetry (0x27/0x2F) is this
    class of command's actual confirmation, same as the amp's own front
    panel/remote has no separate ack for a button press."""
    result = await link.send_command(frame, max_attempts=1)
    return {"ok": True, "confirmed": result.ok, "param": result.param}


class PowerRequest(BaseModel):
    on: bool


@app.post("/api/power")
async def set_power(req: PowerRequest) -> dict:
    link = _require_serial_link()
    if req.on:
        if amp_state.power_state == "off":
            # MODE_OPR_RX is a serial command - it needs the amp's own
            # CPU/UART already powered and listening to parse it, same as
            # the old 0x0A off command could never have turned it back on
            # either. Only the DTR/RTS pulse can wake a fully-off amp
            # (confirmed on real hardware: the serial command alone did
            # nothing from a confirmed-off state). Only pulse here when
            # power_state is confidently "off" (set exclusively by our own
            # prior pulse_power_toggle(), never inferred from comms merely
            # being down) - pulsing a merely-hung-but-still-powered amp
            # would turn it OFF, the opposite of what's wanted.
            await link.pulse_power_toggle()
            # Don't claim "on" yet - we have no confirmation the pulse
            # actually landed until telemetry resumes (mark_comms_ok()
            # will set power_state="on" then). Back to "unknown" rather
            # than leaving it at "off", so a second rapid click during the
            # recovery window doesn't rely on a now-stale "off" belief
            # and double-pulse.
            amp_state.power_state = "unknown"
            # Comms were never up, so _on_comms_change() won't fire its own
            # assume-off timer if the pulse didn't wake the amp - without
            # this, "unknown" stuck forever and Power On returned 409 until
            # a service restart (seen live 2026-10-29 with the amp unplugged).
            _spawn(_assume_off_after_timeout(POWER_ON_ASSUME_OFF_AFTER_S, "a Power On pulse"))
            return {"ok": True, "pulsed": True}
        if amp_state.power_state == "unknown":
            raise HTTPException(
                409,
                "amplifier power state is unknown (not confirmed off) - "
                "cannot safely choose between the mode command and a power pulse",
            )
        return await _send_pulse_command(link, protocol.build_mode_change(protocol.MODE_OPR_RX))

    # Power OFF uses the DTR+RTS pulse (SerialLink.pulse_power_toggle()),
    # not the 0x81/0x02/0x0A software command - that command hangs the
    # amp's own shutdown sequence 100% of the time it's been tried on
    # real hardware, while the pulse has been clean 4/4 (see CLAUDE.md
    # open question 4/5). The pulse is a blind TOGGLE, not a deterministic
    # off, so this only fires when we're confident the amp is actually on
    # right now - without that, we can't tell a pulse-to-off apart from an
    # accidental pulse-to-on.
    if amp_state.power_state == "off":
        # Already off (most likely our own prior pulse). comms_ok can
        # still read stale-True for up to ~10s after that pulse - the
        # watchdog only samples once/sec and needs 10s of silence to flip
        # (CLAUDE.md open question 7) - so without this check, a second
        # rapid Power Off click/retry inside that window would fall
        # through to another blind pulse and actually turn the amp back
        # ON while this code kept right on reporting "off". Treat it as
        # a no-op instead of re-pulsing.
        return {"ok": True, "pulsed": False}
    if amp_state.power_state == "unknown" or not amp_state.comms_ok:
        raise HTTPException(
            409, "cannot safely power off: comms with the amplifier are down, so its current power state is unknown"
        )
    await link.pulse_power_toggle()
    amp_state.mark_power_off_confirmed()
    return {"ok": True, "pulsed": True}


MODE_NAME_TO_VALUE = {
    "debug": protocol.MODE_DEBUG,
    "service": protocol.MODE_SERVICE,
    "stb": protocol.MODE_STB,
    "opr_rx": protocol.MODE_OPR_RX,
    "opr_tx": protocol.MODE_OPR_TX,
    "off": protocol.MODE_OFF,
}


class ModeRequest(BaseModel):
    mode: str
    arm_token: Optional[str] = None


@app.post("/api/mode/arm_tx")
async def arm_tx() -> dict:
    global _tx_arm_token, _tx_arm_expires
    _tx_arm_token = secrets.token_urlsafe(16)
    _tx_arm_expires = time.time() + TX_ARM_WINDOW_S
    backend_logger.info("OPR/TX armed for %.0fs", TX_ARM_WINDOW_S)
    return {"arm_token": _tx_arm_token, "window_s": TX_ARM_WINDOW_S}


@app.post("/api/mode")
async def set_mode(req: ModeRequest) -> dict:
    global _tx_arm_token
    link = _require_serial_link()
    mode_value = MODE_NAME_TO_VALUE.get(req.mode)
    if mode_value is None:
        raise HTTPException(400, f"unknown mode {req.mode!r}; expected one of {sorted(MODE_NAME_TO_VALUE)}")
    if mode_value == protocol.MODE_OPR_TX:
        # Requires possession of the single-use token POST /api/mode/arm_tx
        # just returned - not merely "a client armed TX within the last
        # TX_ARM_WINDOW_S seconds" - so another client/script on the LAN
        # can't ride someone else's confirm-dialog window.
        if (
            _tx_arm_token is None
            or req.arm_token != _tx_arm_token
            or time.time() > _tx_arm_expires
        ):
            raise HTTPException(403, "OPR/TX requires a valid arm_token from POST /api/mode/arm_tx")
        _tx_arm_token = None  # single-use
    return await _send_pulse_command(link, protocol.build_mode_change(mode_value))


class BandRequest(BaseModel):
    direction: str  # "next" | "prev"


@app.post("/api/band")
async def change_band(req: BandRequest) -> dict:
    link = _require_serial_link()
    if req.direction not in ("next", "prev"):
        raise HTTPException(400, "direction must be 'next' or 'prev'")
    action = protocol.BAND_NEXT if req.direction == "next" else protocol.BAND_PREV
    return await _send_pulse_command(link, protocol.build_band_change(action))


class AntennaActionRequest(BaseModel):
    action: str  # only "next" is supported, see CLAUDE.md antenna-cycling note


@app.post("/api/antenna")
async def change_antenna(req: AntennaActionRequest) -> dict:
    link = _require_serial_link()
    if req.action != "next":
        raise HTTPException(
            400,
            "only 'next' is supported - the protocol has no direct "
            "select-antenna-N command for normal tuned operation "
            "(see CLAUDE.md open question 3)",
        )
    return await _send_pulse_command(link, protocol.build_antenna_next())


@app.post("/api/atu/bypass")
async def toggle_atu_bypass() -> dict:
    """Toggles ATU Bypass off the amp's own last-reported atu_label (0x27),
    same pattern as the OPR/STB toggle keying off mode_name - not
    click-tracked UI state. Either direction sends the same Bypass command
    (0x0E/0x02) for the currently selected antenna (see
    protocol.build_atu_bypass's own note: this is the closest thing to a
    direct antenna-select command - CLAUDE.md open question 3).

    Exiting Bypass used to send Full Tune instead, the only "leave Bypass"
    the protocol doc offers - but that always runs a real tuning cycle.
    The amp's own front panel (hold TUNE 1s, a toggle) leaves Bypass by
    recalling the stored tuning instead (logged 2026-10-23: Recalling ->
    Memory Settings, no Matching Cycle). Re-sending Bypass is tried as the
    remote equivalent of that toggle. NEEDS REAL-HARDWARE VERIFICATION -
    so after exiting, this waits briefly and reports `still_bypassed` if
    the amp didn't leave Bypass, for the frontend to say so (Tune is the
    fallback; see /api/atu/tune)."""
    link = _require_serial_link()
    status = amp_state.antenna_band_status
    if status is None:
        raise HTTPException(409, "no antenna/band status received from the amplifier yet")
    exiting = status.atu_label == protocol.ATU_LABEL_BYPASS
    result = await _send_pulse_command(link, protocol.build_atu_bypass(status.antenna_number))
    if not exiting:
        return result
    await asyncio.sleep(BYPASS_EXIT_CHECK_S)
    now = amp_state.antenna_band_status
    still_bypassed = now is not None and now.atu_label == protocol.ATU_LABEL_BYPASS
    if still_bypassed:
        backend_logger.warning("Re-sending Bypass did not take the amp out of ATU Bypass")
    return {**result, "still_bypassed": still_bypassed}


# How long to wait for a 0x27 showing the amp left Bypass. The front panel's
# own exit took ~4s end to end (Recalling -> Memory Settings), but the
# label leaves "ATU In Bypass" (for "Recalling") almost immediately.
BYPASS_EXIT_CHECK_S = 2.5


@app.post("/api/atu/tune")
async def tune() -> dict:
    """Explicit, deliberate Full Tune (0x0E/0x01) - a separate action from
    /api/atu/bypass. Per the protocol doc (section 5.1's ATU Management
    table), Full Tune is the only *documented* way to exit Bypass, so it's
    also the fallback if re-sending Bypass doesn't toggle it off (see
    toggle_atu_bypass()). This endpoint lets a tune be started on demand
    without going through the Bypass toggle at all."""
    link = _require_serial_link()
    return await _send_pulse_command(link, protocol.build_atu_full_tune())


# =============================================================================
# Setup Wizard: serial port enumeration + live reconnect
# =============================================================================


@app.get("/api/serial-ports")
async def get_serial_ports() -> dict:
    return {"ports": list_serial_ports(), "current": config.amp.serial_device}


# =============================================================================
# Setup Wizard: amp/tuner model + serial port (live, no restart)
# =============================================================================


VALID_LOG_LEVELS = ("INFO", "DEBUG")


class ConfigRequest(BaseModel):
    # Both Optional with no default so `exclude_unset` can distinguish
    # "field omitted - don't touch it" from "field explicitly sent (even
    # null) - apply it" - see set_config().
    amp_model: Optional[str] = None
    tuner_model: Optional[str] = None
    serial_device: Optional[str] = None
    # "DEBUG" turns on verbose TX/RX frame + retry logging (see
    # serial_link.py) - off by default since it's noisy for 24/7
    # operation, toggled live from the Setup screen rather than requiring
    # a config.json edit + restart.
    logging_level: Optional[str] = None
    # Must be explicitly true to change serial_device - switching ports
    # closes/reopens the OS-level handle, which risks toggling the amp's
    # power via DTR (see CLAUDE.md open question 4) - this is enforced
    # here, not just in the frontend's confirm() dialog, so no caller can
    # trigger it silently.
    confirm: bool = False


def _config_response() -> dict:
    return {
        "amp_model": config.amp.model,
        "tuner_model": config.tuner.model,
        "serial_device": config.amp.serial_device,
        "debug_logging_enabled": config.logging.level == "DEBUG",
        "log_marker": config.logging.log_marker,
        "git_commit": GIT_COMMIT,
        "comms_ok": amp_state.comms_ok,
        "available_amp_models": sorted(AMP_PROFILES),
        "available_tuner_models": list(TUNER_MODELS),
        "display": config.display.panels,
        "brightness": config.display.brightness,
        "brightness_min": DISPLAY_BRIGHTNESS_MIN,
        "backlight_available": _backlight_device() is not None,
        "kiosk": _kiosk_status(),
    }


@app.get("/api/config")
async def get_config() -> dict:
    return _config_response()


@app.post("/api/config")
async def set_config(req: ConfigRequest) -> dict:
    # Partial-update semantics: a field not present in the request body at
    # all leaves the current value untouched; a field explicitly sent
    # (including `null`) is applied. Without this, a caller that only
    # means to change amp_model would silently clear tuner_model back to
    # None, since that's the field's default.
    provided = req.model_dump(exclude_unset=True)
    new_amp_model = provided.get("amp_model", config.amp.model)
    new_tuner_model = provided["tuner_model"] if "tuner_model" in provided else config.tuner.model
    new_serial_device = provided.get("serial_device", config.amp.serial_device)
    new_logging_level = provided.get("logging_level", config.logging.level)

    try:
        validate_config(new_amp_model, new_tuner_model)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if new_logging_level not in VALID_LOG_LEVELS:
        raise HTTPException(400, f"logging_level must be one of {VALID_LOG_LEVELS}")

    device_changed = new_serial_device != config.amp.serial_device
    if device_changed and not req.confirm:
        raise HTTPException(400, "must set confirm=true to change the serial port (may toggle amp power)")

    if device_changed:
        link = _require_serial_link()
        try:
            await link.reconfigure(new_serial_device)
        except Exception as exc:
            # Reopen failed (bad/missing port) - don't persist the bad path;
            # leave config pointing at the last-known-good device.
            raise HTTPException(502, f"Failed to open {new_serial_device!r}: {exc}") from exc

    if new_logging_level != config.logging.level:
        logging.getLogger().setLevel(getattr(logging, new_logging_level))
        backend_logger.info("Logging level changed: %s -> %s", config.logging.level, new_logging_level)

    config.amp.model = new_amp_model
    config.tuner.model = new_tuner_model
    config.logging.level = new_logging_level
    if device_changed:
        config.amp.serial_device = new_serial_device
    amp_state.profile = get_amp_profile(new_amp_model)
    amp_state.tuner_model = new_tuner_model
    config.save()
    backend_logger.info(
        "Config updated live: amp=%s tuner=%s serial_device=%s logging=%s",
        new_amp_model, new_tuner_model, config.amp.serial_device, new_logging_level,
    )
    return _config_response()


# =============================================================================
# Setup Wizard: dashboard Display layout (shared by every device)
# =============================================================================


class DisplayPanelRequest(BaseModel):
    id: str
    visible: bool = True
    width: str = "half"


class DisplayRequest(BaseModel):
    panels: list[DisplayPanelRequest]


@app.post("/api/display")
async def set_display(req: DisplayRequest) -> dict:
    """Saves the Setup screen's Display layout (panel order, visibility,
    width). Unknown ids are rejected outright; anything else odd (missing
    panels, a bad width, the band panel set to less than full width) is
    normalized rather than refused - see config.normalize_display_panels()."""
    unknown = [p.id for p in req.panels if p.id not in {pid for pid, _ in DISPLAY_PANEL_DEFAULTS}]
    if unknown:
        raise HTTPException(400, f"unknown display panel id(s): {unknown}")
    config.display.panels = normalize_display_panels([p.model_dump() for p in req.panels])
    config.save()
    return {"display": config.display.panels}


# =============================================================================
# Pi display backlight: Setup -> Display's brightness slider, and the kiosk's
# inactivity dimming (app.js's initIdleDim() - the kiosk page decides when to
# dim, since it's the one that sees touches and key-ups; this just drives the
# hardware). The official touchscreen exposes /sys/class/backlight/<dev>;
# an HDMI monitor has nothing there, and the kiosk falls back to dimming
# the page itself. Writing it needs the video group - see
# setup/acom-control.service's SupplementaryGroups.
# =============================================================================

BACKLIGHT_DIR = Path("/sys/class/backlight")
BACKLIGHT_DIM_PERCENT = 5
_backlight_dimmed = False


def _backlight_device() -> Optional[Path]:
    try:
        devices = sorted(BACKLIGHT_DIR.iterdir())
    except OSError:
        return None
    return devices[0] if devices else None


def _apply_backlight() -> bool:
    """Sets the backlight to the dim level or the saved brightness. Returns
    whether that worked, so the kiosk knows to dim the page itself instead."""
    device = _backlight_device()
    if device is None:
        return False
    percent = BACKLIGHT_DIM_PERCENT if _backlight_dimmed else config.display.brightness
    try:
        max_level = int((device / "max_brightness").read_text())
        level = max(1, round(max_level * percent / 100))
        (device / "brightness").write_text(f"{level}\n")
    except (OSError, ValueError) as exc:
        backend_logger.warning("Could not set the Pi display backlight (%s): %s", device, exc)
        return False
    return True


class BrightnessRequest(BaseModel):
    brightness: int


@app.post("/api/display/brightness")
async def set_display_brightness(req: BrightnessRequest) -> dict:
    """Saves the slider's brightness and applies it right away (unless the
    screen is currently dimmed - then it applies on the next wake)."""
    config.display.brightness = normalize_display_brightness(req.brightness)
    config.save()
    applied = _apply_backlight()
    return {"brightness": config.display.brightness, "backlight": applied}


class BacklightRequest(BaseModel):
    dimmed: bool


@app.post("/api/display/backlight")
async def set_display_backlight(req: BacklightRequest) -> dict:
    """The kiosk's inactivity dim/wake. Not saved - a restart comes back at
    the saved brightness, and the kiosk page re-sends this when it loads."""
    global _backlight_dimmed
    _backlight_dimmed = req.dimmed
    return {"backlight": _apply_backlight()}


# =============================================================================
# Setup Wizard: debug log export
# =============================================================================

LOG_UNIT_NAME = "acom-control"
LOG_EXPORT_LINES = 20000


@app.get("/api/logs")
async def download_logs() -> Response:
    """Exports recent backend logs for offline analysis (see
    PI_CONTROL_PLAN.md's "Download logs from the UI" note) - everything
    goes to stdout -> journald (no app-managed log file exists, see
    "Backend vs. frontend logging"), so this just shells out to
    journalctl rather than reading a file. Requires the acom-control
    system user to be in the systemd-journal group - see
    setup/acom-control.service's install comment.

    If a log_marker is set (POST /api/logs/clear), only exports entries
    from that point forward instead of the usual tail - keeps a long-
    running service's downloads focused on what's currently going on
    rather than growing to cover its entire uptime."""
    args = ["journalctl", "-u", LOG_UNIT_NAME, "--no-pager"]
    if config.logging.log_marker:
        args += ["--since", config.logging.log_marker]
    args += ["-n", str(LOG_EXPORT_LINES)]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise HTTPException(502, f"journalctl failed: {stderr.decode(errors='replace')}")
    filename = f"acom-control-{time.strftime('%Y%m%d-%H%M%S')}.log"
    return Response(
        content=stdout,
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/logs/clear")
async def clear_logs() -> dict:
    """Doesn't delete anything from journald (the acom-control user only
    has read access, see download_logs()'s docstring) - just records a
    marker timestamp so future GET /api/logs downloads start from here
    instead of the usual tail, which also keeps those downloads small
    for a service that's been running a long time."""
    marker = time.strftime("%Y-%m-%d %H:%M:%S")
    config.logging.log_marker = marker
    config.save()
    backend_logger.info("Log view cleared - future exports will only include entries after %s", marker)
    return {"ok": True, "log_marker": marker}


# =============================================================================
# Self-update: a header button that checks on click (plus the same check
# once a day in the background, journalctl-visible even though nothing
# in the UI polls for it) and the apply action behind its popover's
# "Update Now" button. Built because running multi-line SSH commands
# from a phone is a pain - both of these shell out (via sudo) to the
# single fixed, install.sh-provisioned helper script at
# UPDATE_APPLY_SCRIPT; see setup/acom-update-apply.sh and
# setup/acom-control-update.sudoers for exactly what it's allowed to do
# and why. Nothing here runs with any more privilege than that one
# script grants - this process itself stays unprivileged. Visible/
# actionable by anyone with the dashboard open - there's no auth in this
# app yet (per-user login is a known future item, not built).
# =============================================================================

UPDATE_APPLY_SCRIPT = "/usr/local/bin/acom-update-apply"
UPDATE_CHECK_TIMEOUT_S = 30.0
UPDATE_CHECK_INTERVAL_S = 24 * 60 * 60.0
UPDATE_CHECK_STARTUP_DELAY_S = 30.0  # let the startup handshake settle first

# Mutable, not a dataclass - this is purely informational UI state (not
# amp hardware state, so it doesn't belong in AmpState/amp_state.py,
# which ModbusServer also reads from and has no use for this), and only
# ever touched from this module's own asyncio loop, so no locking needed.
update_status: dict = {
    "available": False,
    "current_version": GIT_VERSION,
    "latest_version": None,
    "checked_at": None,
    "error": None,
    "changes": [],
    "beta": False,
    # Set when beta updates are off but the Pi is running a beta build
    # (ahead of the latest release tag) - the update screen then offers
    # rolling back to `release` (POST /api/update/rollback).
    "can_rollback": False,
    "release": None,
}

_update_check_task: Optional[asyncio.Task] = None

CHANGELOG_PATH = REPO_ROOT / "CHANGELOG.md"
CHANGELOG_VERSION_HEADER_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\](?:\s*-\s*(.+))?$", re.MULTILINE)


def _version_tuple(version: str) -> tuple[int, int, int]:
    """Best-effort MAJOR.MINOR.PATCH parse. Strips a leading "v" and any
    `git describe`-style "-N-gHASH" suffix (current_version can be e.g.
    "v0.1.2-3-gabc1234" when ahead of the last tag) - unparseable input
    (no tags yet, "unknown") falls back to (0, 0, 0) rather than raising,
    since this only ever feeds a "which changelog entries are newer than
    this" comparison, not something that needs to be exact."""
    bare = version.lstrip("v").split("-")[0]
    parts = bare.split(".")
    try:
        return tuple(int(p) for p in (parts + ["0", "0"])[:3])  # type: ignore[return-value]
    except ValueError:
        return (0, 0, 0)


def _parse_changelog_entries(text: str) -> list[dict]:
    """Splits a CHANGELOG.md (Keep a Changelog style) into per-version
    sections keyed by `## [X.Y.Z] - date` headers - deliberately ignores
    `[Unreleased]` (not a released version, and nothing a Pi update
    would ever land on) and anything else that doesn't match that exact
    shape, rather than trying to be a general markdown parser."""
    matches = list(CHANGELOG_VERSION_HEADER_RE.finditer(text))
    entries = []
    for i, m in enumerate(matches):
        version, date = m.group(1), (m.group(2) or "").strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        entries.append({"version": version, "date": date, "body": text[start:end].strip()})
    return entries


async def _read_changelog_at_ref(ref: str) -> str:
    """Reads CHANGELOG.md as of `ref` (a tag) straight from the local git
    object database - not the working tree, which is still whatever's
    currently checked out (the whole point of `check` mode is to never
    touch it). Purely local once the tag's objects are fetched (already
    done by the helper's own `check`/apply modes), so this runs directly
    as this unprivileged process - no sudo needed, unlike the fetch/pull/
    restart steps that need REPO_OWNER's network credentials or root."""
    proc = await asyncio.create_subprocess_exec(
        *GIT_CMD, "show", f"{ref}:CHANGELOG.md",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode(errors="replace").strip() or "git show failed")
    return stdout.decode(errors="replace")


async def _read_commit_subjects(ref: str) -> list[str]:
    """Beta-channel stand-in for changelog entries: untagged commits on
    main usually have no CHANGELOG.md section yet, so list the commit
    subjects between HEAD and `ref` instead (newest first). Local-only,
    same as _read_changelog_at_ref() - the helper's check already
    fetched the objects."""
    proc = await asyncio.create_subprocess_exec(
        *GIT_CMD, "log", "--format=%s", f"HEAD..{ref}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode(errors="replace").strip() or "git log failed")
    return [line for line in stdout.decode(errors="replace").splitlines() if line.strip()]


async def _run_update_helper(*args: str, timeout: float) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "sudo", UPDATE_APPLY_SCRIPT, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


# One update check at a time. The dashboard can fire two at once (opening
# the update screen while another check is still running), and two helper
# runs doing `git fetch` on the same repo collide - seen on the Pi as
# "cannot lock ref 'refs/remotes/origin/main'". A second request now waits
# for the first, then runs its own check (so it still sees the current
# beta setting).
_update_check_lock = asyncio.Lock()


async def _check_for_update() -> None:
    async with _update_check_lock:
        await _check_for_update_locked()


async def _check_for_update_locked() -> None:
    """Runs the helper's read-only `check` mode (git fetch + compare,
    never pulls or restarts - see acom-update-apply.sh) and records the
    result in `update_status` for the dashboard to show. Failures (no
    network, auth broken, etc.) are logged and recorded but never raised
    - this runs unattended on a timer, so there's no one to show an
    exception to."""
    beta = config.update.beta
    update_status["changes"] = []
    update_status["beta"] = beta
    try:
        returncode, stdout, stderr = await _run_update_helper(
            "check-beta" if beta else "check", timeout=UPDATE_CHECK_TIMEOUT_S
        )
        if returncode != 0:
            raise RuntimeError(stderr.strip() or f"exited {returncode}")
        fields = dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line)
        update_status["available"] = fields.get("update_available") == "yes"
        update_status["current_version"] = fields.get("current", GIT_VERSION)
        latest = fields.get("latest", "none")
        update_status["latest_version"] = latest if latest != "none" else None
        release = fields.get("release", "none")
        update_status["release"] = release if release != "none" else None
        # An older installed helper doesn't print ahead_of_release, which
        # reads as "no" - no rollback offered until install.sh is re-run.
        update_status["can_rollback"] = not beta and fields.get("ahead_of_release") == "yes"
        update_status["error"] = None
        if update_status["available"]:
            backend_logger.info(
                "Update available%s: %s -> %s", " (beta)" if beta else "",
                update_status["current_version"], update_status["latest_version"],
            )
            try:
                if beta:
                    subjects = await _read_commit_subjects(fields.get("latest_ref", "origin/main"))
                    update_status["changes"] = [{
                        "title": f"{len(subjects)} new commit{'s' if len(subjects) != 1 else ''} on main",
                        "body": "\n".join(f"- {s}" for s in subjects),
                    }]
                else:
                    changelog_text = await _read_changelog_at_ref(update_status["latest_version"])
                    current_v = _version_tuple(update_status["current_version"])
                    latest_v = _version_tuple(update_status["latest_version"])
                    update_status["changes"] = [
                        entry for entry in _parse_changelog_entries(changelog_text)
                        if current_v < _version_tuple(entry["version"]) <= latest_v
                    ]
            except Exception as exc:
                # Not fatal to the check itself - "update available" is
                # still correct even if we can't show what's in it.
                backend_logger.warning("Could not load changelog entries for %s: %s",
                                        update_status["latest_version"], exc)
    except Exception as exc:
        update_status["error"] = str(exc)
        backend_logger.warning("Update check failed: %s", exc)
    finally:
        update_status["checked_at"] = time.time()


async def _update_check_loop() -> None:
    await asyncio.sleep(UPDATE_CHECK_STARTUP_DELAY_S)
    while True:
        await _check_for_update()
        await asyncio.sleep(UPDATE_CHECK_INTERVAL_S)


@app.get("/api/update/status")
async def get_update_status() -> dict:
    """Cached result of the daily background check (_update_check_loop) -
    cheap, just reads an in-memory dict, no git calls on this request
    path. Drives the header icon's highlighted/bouncing vs. plain state
    between popover opens, so the icon can get your attention on its
    own instead of requiring a click first."""
    update_status["beta"] = config.update.beta  # correct even before the first check runs
    return dict(update_status)


@app.post("/api/update/check")
async def check_for_update_now() -> dict:
    """On-demand version of the same daily background check - the header
    icon's popover calls this every time it's opened, so "Update Now" is
    always backed by a fresh answer rather than whatever the daily timer
    last happened to see (which could be hours stale, or simply hasn't
    run yet on a freshly restarted service). Read-only, never pulls or
    restarts."""
    await _check_for_update()
    return dict(update_status)


class UpdateBetaRequest(BaseModel):
    beta: bool


@app.post("/api/update/beta")
async def set_update_beta(req: UpdateBetaRequest) -> dict:
    """Update screen's "beta updates" toggle - persists the choice, then
    re-runs the check right away so the screen reflects the new channel
    without a second round-trip."""
    if req.beta != config.update.beta:
        config.update.beta = req.beta
        config.save()
        backend_logger.info("Beta updates %s", "enabled" if req.beta else "disabled")
    await _check_for_update()
    return dict(update_status)


@app.post("/api/update/apply")
async def apply_update() -> dict:
    """Launches the helper's default (no-arg) mode - pulls, reinstalls
    requirements if changed, and restarts the service - see
    acom-update-apply.sh. Detached (not awaited): `systemctl restart`
    kills this very process partway through, so this returns as soon as
    the update is *launched*, not once it's finished. The frontend
    should watch /api/config's git_commit for the restart actually
    landing, not expect this request to stay alive for it."""
    beta = config.update.beta
    try:
        subprocess.Popen(
            ["sudo", UPDATE_APPLY_SCRIPT, *(["beta"] if beta else [])],
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    except Exception as exc:
        raise HTTPException(500, f"failed to launch update: {exc}") from exc
    backend_logger.info("Update requested from the dashboard%s (currently at commit %s)",
                        " (beta)" if beta else "", GIT_COMMIT)
    return {"ok": True, "launched": True, "current_commit": GIT_COMMIT}


# =============================================================================
# Kiosk display: the dashboard full screen on the Pi's own screen (Setup ->
# Display's "Raspberry Pi display" switch). Turning it on/off needs root -
# installing cage/Chromium the first time, enabling the service - so, like
# updates, it goes through the helper's fixed kiosk-on/kiosk-off modes (see
# setup/acom-update-apply.sh). Status is read unprivileged from systemd and
# /sys.
# =============================================================================

KIOSK_UNIT = "acom-kiosk.service"
_kiosk_job: Optional[subprocess.Popen] = None  # the running/last kiosk-on/off helper run
_kiosk_job_wanted: Optional[bool] = None


def _systemctl_query(verb: str) -> str:
    try:
        result = subprocess.run(["systemctl", verb, KIOSK_UNIT], capture_output=True, text=True, timeout=3)
        return result.stdout.strip()
    except Exception:
        return ""


def _connected_displays() -> list[str]:
    """Names of the screens the Pi currently sees as plugged in (e.g.
    "HDMI-A-1", "DSI-1" for the official touchscreen), from the kernel's
    DRM connector status - so the Setup screen can say whether there's
    anything to show the kiosk on."""
    names = []
    try:
        for status in Path("/sys/class/drm").glob("card*-*/status"):
            if status.read_text().strip() == "connected":
                names.append(status.parent.name.split("-", 1)[1])
    except OSError:
        pass
    return sorted(names)


def _kiosk_status() -> dict:
    enabled_state = _systemctl_query("is-enabled")  # "enabled"/"disabled"/"" when not installed
    busy = _kiosk_job is not None and _kiosk_job.poll() is None
    failed = _kiosk_job is not None and not busy and _kiosk_job.returncode != 0
    return {
        "installed": enabled_state in ("enabled", "disabled", "static", "indirect"),
        "enabled": enabled_state == "enabled",
        "active": _systemctl_query("is-active") == "active",
        "displays": _connected_displays(),
        "busy": busy,
        # The last switch flip ended with an error (see the log download).
        "failed": failed,
        "wanted": _kiosk_job_wanted,
    }


class KioskRequest(BaseModel):
    enabled: bool


@app.post("/api/kiosk")
async def set_kiosk(req: KioskRequest) -> dict:
    """Turns the kiosk display on or off via the helper. Detached - the
    first kiosk-on installs cage and Chromium, which takes minutes; the
    frontend polls GET /api/config's "kiosk" until it settles. Its output
    goes to this service's journal (log download)."""
    global _kiosk_job, _kiosk_job_wanted
    if _kiosk_job is not None and _kiosk_job.poll() is None:
        raise HTTPException(409, "the Pi display is still being switched - wait for it to finish")
    try:
        _kiosk_job = subprocess.Popen(
            ["sudo", UPDATE_APPLY_SCRIPT, "kiosk-on" if req.enabled else "kiosk-off"],
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    except Exception as exc:
        raise HTTPException(500, f"failed to switch the Pi display: {exc}") from exc
    _kiosk_job_wanted = req.enabled
    backend_logger.info("Pi display (kiosk) turned %s from the dashboard", "on" if req.enabled else "off")
    return {"ok": True, "kiosk": _kiosk_status()}


@app.post("/api/update/rollback")
async def rollback_update() -> dict:
    """Launches the helper's `rollback` mode: moves a Pi running a beta
    build back to the latest release, reinstalls requirements and restarts
    (see acom-update-apply.sh). Only offered with beta updates off - with
    them on, the next check would just offer the beta build again. Same
    detached launch as apply_update(); the frontend watches git_commit for
    the restart."""
    if config.update.beta:
        raise HTTPException(409, "turn off beta updates before rolling back to a release")
    try:
        subprocess.Popen(
            ["sudo", UPDATE_APPLY_SCRIPT, "rollback"],
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    except Exception as exc:
        raise HTTPException(500, f"failed to launch rollback: {exc}") from exc
    backend_logger.info("Rollback to release %s requested from the dashboard (currently at commit %s)",
                        update_status.get("release"), GIT_COMMIT)
    return {"ok": True, "launched": True, "current_commit": GIT_COMMIT}


@app.get("/changelog")
async def get_changelog() -> Response:
    """Full CHANGELOG.md as currently checked out, for the update
    screen's "View full changelog" link (opens in a new tab) - plain
    text rather than rendering markdown, no renderer dependency needed
    for something this simple."""
    try:
        text = CHANGELOG_PATH.read_text()
    except FileNotFoundError:
        raise HTTPException(404, "CHANGELOG.md not found")
    return Response(content=text, media_type="text/plain")


# =============================================================================
# Antenna Setup: names + band/type matrix
# =============================================================================


class AntennaSlotRequest(BaseModel):
    number: int
    name: str = ""
    type: str = "Sharp"
    bands: list[str] = []


class AntennaConfigRequest(BaseModel):
    antennas: list[AntennaSlotRequest]


@app.get("/api/antenna/config")
async def get_antenna_config() -> dict:
    return {"antennas": [slot.to_dict() for slot in amp_state.antenna_config]}


@app.post("/api/antenna/config")
async def set_antenna_config(req: AntennaConfigRequest) -> dict:
    link = _require_serial_link()
    if len(req.antennas) != protocol.NUM_ANTENNAS:
        raise HTTPException(400, f"expected {protocol.NUM_ANTENNAS} antenna entries")
    try:
        slots = [protocol.antenna_slot_from_dict(a.model_dump()) for a in req.antennas]
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    ordered = sorted(slots, key=lambda s: s.number)
    names = [s.name for s in ordered]

    # The amp answers these writes by sending back the stored table (0x1B
    # for names - confirmed on real hardware, never an OK), so that
    # read-back counts as confirmation. 0x18 for assignments is assumed to
    # work the same way; an OK still counts if it sends one instead.
    names_result = await link.send_command(protocol.build_set_antenna_names(names), echo_address=0x1B)
    if not names_result.ok:
        raise HTTPException(502, f"amplifier did not confirm antenna names (param=0x{names_result.param:04X})")
    assignments_result = await link.send_command(
        protocol.build_set_antenna_assignments(ordered), echo_address=0x18
    )
    if not assignments_result.ok:
        raise HTTPException(502, f"amplifier did not confirm antenna assignments (param=0x{assignments_result.param:04X})")
    # Refresh AmpState from the amp's own echo rather than trusting the
    # write blindly - see PI_CONTROL_PLAN.md "Setup Wizard & Antenna Setup".
    # These are status queries (reply is the requested message itself, not
    # an OK/ERR), so they stay fire-and-forget.
    await link.enqueue(protocol.build_request_message(0x18))
    await asyncio.sleep(0.01)
    await link.enqueue(protocol.build_request_message(0x1B))

    backend_logger.info("Antenna Setup saved: %s", [s.to_dict() for s in ordered])
    return {"ok": True}


# =============================================================================
# CAT / Rig Control (message 0x12 get, 0x05/0x06/0x07 set) - lets the
# Setup screen show and change what the amp's own rear-panel CAT port
# is configured to talk to (interface type, command set/"library",
# baud rate, byte spacing, polling time), same settings the amp's own
# front panel exposes under its CAT menu.
# =============================================================================


class CatSettingsRequest(BaseModel):
    interface_type: int
    command_set: int
    baud_rate: int
    byte_spacing_us: int
    polling_time_ms: int


@app.post("/api/cat-settings")
async def set_cat_settings(req: CatSettingsRequest) -> dict:
    link = _require_serial_link()
    if not 0 <= req.interface_type <= 0xF:
        raise HTTPException(400, "interface_type must be 0-15")
    if not 0 <= req.command_set <= 0xF:
        raise HTTPException(400, "command_set must be 0-15")
    if not 0 <= req.baud_rate <= 0xF:
        raise HTTPException(400, "baud_rate must be 0-15")
    if not 0 <= req.byte_spacing_us <= 0xFFFF:
        raise HTTPException(400, "byte_spacing_us must be 0-65535")
    if not 0 <= req.polling_time_ms <= 0xFFFF:
        raise HTTPException(400, "polling_time_ms must be 0-65535")

    result = await link.send_command(
        protocol.build_cat_settings_1(req.interface_type, req.command_set, req.baud_rate)
    )
    if not result.ok:
        raise HTTPException(502, f"amplifier did not confirm CAT Settings 1 (param=0x{result.param:04X})")
    result = await link.send_command(protocol.build_cat_settings_2(req.byte_spacing_us))
    if not result.ok:
        raise HTTPException(502, f"amplifier did not confirm CAT Settings 2 (param=0x{result.param:04X})")
    result = await link.send_command(protocol.build_cat_settings_3(req.polling_time_ms))
    if not result.ok:
        raise HTTPException(502, f"amplifier did not confirm CAT Settings 3 (param=0x{result.param:04X})")

    # Refresh AmpState from the amp's own echo rather than trusting the
    # write blindly - same pattern as Antenna Setup above. Status query
    # (reply is the requested message itself, not an OK/ERR), so it
    # stays fire-and-forget.
    await link.enqueue(protocol.build_request_message(0x12))

    backend_logger.info(
        "CAT settings saved: interface_type=%d command_set=%d baud_rate=%d byte_spacing_us=%d polling_time_ms=%d",
        req.interface_type, req.command_set, req.baud_rate, req.byte_spacing_us, req.polling_time_ms,
    )
    return {"ok": True}


# =============================================================================
# Buzzer, frontend error log, destructive (web-UI-only) commands
# =============================================================================


class BuzzerRequest(BaseModel):
    signal: int = protocol.BUZZER_PULSE
    volume: int = 50


@app.post("/api/buzzer")
async def test_buzzer(req: BuzzerRequest) -> dict:
    # Button-style: the amp beeps but never ACKs it, so the retrying
    # send_command() path beeped up to 5 times and then returned 502.
    link = _require_serial_link()
    return await _send_pulse_command(link, protocol.build_buzzer(req.signal, req.volume))


BUZZER_MORSE_TEXT = "K7TDM TEST"
# The amp's own startup "TEST" sounds like ~20 WPM (first guessed at 15,
# which per Tyler sounded too slow). Timed a bit slower than that here, so
# a character is never sent while the amp is still playing the previous
# one (it drops it - see protocol.morse_schedule()).
BUZZER_MORSE_WPM = 20
BUZZER_MORSE_MARGIN = 1.15

_morse_task: Optional[asyncio.Task] = None


class BuzzerMorseRequest(BaseModel):
    volume: int = 50


async def _play_morse(link: SerialLink, schedule: list) -> None:
    for frame, wait_s in schedule:
        if frame is not None:
            await link.enqueue(frame)
        await asyncio.sleep(wait_s * BUZZER_MORSE_MARGIN)


@app.post("/api/buzzer/morse")
async def buzzer_morse(req: BuzzerMorseRequest) -> dict:
    """Plays BUZZER_MORSE_TEXT in Morse on the amp's buzzer: one 0x0A
    command per character, each sent only after the previous one has had
    time to play (the amp doesn't buffer them). Runs in the background and
    returns at once; a click while it's still playing is ignored. Never
    ACKed, like the other buzzer signals, so these are fire-and-forget."""
    global _morse_task
    link = _require_serial_link()
    if _morse_task is not None and not _morse_task.done():
        return {"ok": True, "text": BUZZER_MORSE_TEXT, "already_playing": True}
    schedule = protocol.morse_schedule(BUZZER_MORSE_TEXT, max(0, min(100, req.volume)), BUZZER_MORSE_WPM)
    _morse_task = _spawn(_play_morse(link, schedule))
    backend_logger.info("Buzzer Morse: %r", BUZZER_MORSE_TEXT)
    return {"ok": True, "text": BUZZER_MORSE_TEXT}


class FrontendLogRequest(BaseModel):
    level: str = "error"
    message: str
    stack: Optional[str] = None
    url: Optional[str] = None


@app.post("/api/frontend-log", status_code=204)
async def frontend_log(req: FrontendLogRequest, request: Request) -> None:
    client_host = request.client.host if request.client else "unknown"
    log_fn = getattr(frontend_logger, req.level.lower(), None) or frontend_logger.error
    log_fn("[%s] %s | url=%s stack=%s", client_host, req.message, req.url, req.stack)


FACTORY_RESET_NAME_TO_VALUE = {
    "user_prefs": protocol.FACTORY_RESET_USER_PREFS,
    "hour_counter": protocol.FACTORY_RESET_HOUR_COUNTER,
    "error_log": protocol.FACTORY_RESET_ERROR_LOG,
    "all": protocol.FACTORY_RESET_ALL,
    "antenna_assignments": protocol.FACTORY_RESET_ANTENNA_ASSIGNMENTS,
}


class FactoryResetRequest(BaseModel):
    selection: str
    confirm: bool = False


@app.post("/api/factory-reset")
async def factory_reset(req: FactoryResetRequest) -> dict:
    """Destructive - web-UI-only per PI_CONTROL_PLAN.md, never exposed via
    modbus_server.py. (Renamed from the old /api/atu/erase - this endpoint
    only ever implemented 0x81 Cmd 0x0D "Factory Reset", not the ATU
    memory-erase commands; see /api/atu/erase-memory below for those.)"""
    link = _require_serial_link()
    if not req.confirm:
        raise HTTPException(400, "must set confirm=true to perform a destructive reset")
    value = FACTORY_RESET_NAME_TO_VALUE.get(req.selection)
    if value is None:
        raise HTTPException(400, f"unknown selection {req.selection!r}; expected one of {sorted(FACTORY_RESET_NAME_TO_VALUE)}")
    backend_logger.warning("Destructive factory-reset requested: %s", req.selection)
    result = await link.send_command(protocol.build_factory_reset(value))
    return _require_confirmed(result)


# =============================================================================
# ATU tuning-record lookup / delete / memory erase (Antenna Setup "Danger
# Zone" - rarely used, destructive, gated behind confirm + frontend
# double-confirmation; see PI_CONTROL_PLAN.md "Setup Wizard & Antenna
# Setup").
# =============================================================================


class AtuTuningRecordRequest(BaseModel):
    antenna_number: int
    band_number: int
    record_number: int = 0


@app.post("/api/atu/tuning-record")
async def request_atu_tuning_record(req: AtuTuningRecordRequest) -> dict:
    """Requests a specific ATU tuning record (0x81 Cmd 0x0F). This is a
    status query - the reply is message 0x1A itself (see
    protocol.parse_atu_segment_tuning), not an OK/ERR, so it stays
    fire-and-forget like build_request_message."""
    link = _require_serial_link()
    try:
        frame = protocol.build_request_atu_tuning_record(req.antenna_number, req.band_number, req.record_number)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await link.enqueue(frame)
    return {"ok": True}


class DeleteSegmentTuningRequest(BaseModel):
    antenna_number: int
    band_number: int
    record_number: int = 0
    confirm: bool = False


@app.post("/api/atu/delete-segment-tuning")
async def delete_segment_tuning(req: DeleteSegmentTuningRequest) -> dict:
    """Destructive - erases one stored ATU tuning entry (0x81 Cmd 0x12).
    Web-UI-only, gated behind confirm, never exposed via modbus_server.py."""
    link = _require_serial_link()
    if not req.confirm:
        raise HTTPException(400, "must set confirm=true to delete a tuning entry")
    try:
        frame = protocol.build_delete_segment_tuning(req.antenna_number, req.band_number, req.record_number)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    backend_logger.warning(
        "Deleting segment tuning: antenna=%d band=%d record=%d",
        req.antenna_number, req.band_number, req.record_number,
    )
    result = await link.send_command(frame)
    return _require_confirmed(result)


ATU_ERASE_NAME_TO_VALUE = {
    "current": protocol.ATU_ERASE_CURRENT,
    "specific": protocol.ATU_ERASE_SPECIFIC_ANTENNA,
    "all": protocol.ATU_ERASE_ALL,
    "whole_memory": protocol.ATU_ERASE_WHOLE_MEMORY,
}


class AtuEraseMemoryRequest(BaseModel):
    selection: str
    antenna_number: Optional[int] = None
    confirm: bool = False


@app.post("/api/atu/erase-memory")
async def erase_atu_memory(req: AtuEraseMemoryRequest) -> dict:
    """Destructive - erases stored ATU tuning memory (0x81 Cmd 0x10).
    Web-UI-only (gated behind the Antenna Setup "Danger Zone" + confirm),
    never exposed via modbus_server.py. Used rarely by design - see
    PI_CONTROL_PLAN.md."""
    link = _require_serial_link()
    if not req.confirm:
        raise HTTPException(400, "must set confirm=true to erase ATU memory")
    value = ATU_ERASE_NAME_TO_VALUE.get(req.selection)
    if value is None:
        raise HTTPException(400, f"unknown selection {req.selection!r}; expected one of {sorted(ATU_ERASE_NAME_TO_VALUE)}")
    if req.selection == "specific" and not req.antenna_number:
        raise HTTPException(400, "antenna_number is required when selection='specific'")
    try:
        frame = protocol.build_atu_erase_memory(value, req.antenna_number or 0)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    backend_logger.warning("Destructive ATU memory erase requested: %s (antenna=%s)", req.selection, req.antenna_number)
    result = await link.send_command(frame)
    return _require_confirmed(result)


@app.get("/", include_in_schema=False)
async def get_index() -> Response:
    """Serves index.html with its asset URLs (app.js, styles/*.css)
    cache-busted to the current version (?v=v0.1.3 etc.) instead of
    plain filenames - a mismatched cached app.js/index.html pair
    (stale JS referencing elements a fresh HTML page doesn't have, or
    vice versa) was observed on real hardware to fail completely
    silently (no visible error, button clicks just do nothing). Every
    version gets its own asset URLs the browser has never cached
    before, so there's nothing to go stale - no more manually clearing
    site data after an update. This route (an exact match on "/") is
    registered before the StaticFiles mount below and so takes
    priority over it for this one path; everything else (app.js,
    styles/*, etc.) still comes straight from disk via that mount,
    unversioned - only the HTML shell needs this treatment, since it's
    the one thing that assigns the version-tagged URLs those files are
    fetched through.

    Explicitly never cached itself (Cache-Control: no-store) - if this
    shell were ever served stale, it would keep pointing at an old
    asset version forever, defeating the whole point."""
    html = (FRONTEND_DIR / "index.html").read_text().replace("{{ASSET_VERSION}}", GIT_VERSION)
    return Response(content=html, media_type="text/html", headers={"Cache-Control": "no-store"})


# Mounted last so it doesn't shadow the /api, /ws, and / routes registered above.
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
