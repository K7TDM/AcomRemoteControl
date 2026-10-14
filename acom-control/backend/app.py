"""FastAPI app: HTTP/WebSocket API, static dashboard, startup/shutdown
wiring. See PI_CONTROL_PLAN.md step 4 for the endpoint list this
implements, and "Backend vs. frontend logging" for the two-logger design.

Run during development with:
    uvicorn backend.app:app --host 0.0.0.0 --port 8000
(from the acom-control/ directory, with the venv's packages installed).
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import protocol
from .amp_profiles import AMP_PROFILES, TUNER_MODELS, get_amp_profile
from .amp_state import AmpState
from .config import AppConfig, load_config, validate_config
from .modbus_server import ModbusServer
from .serial_link import SerialLink, list_serial_ports

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

backend_logger = logging.getLogger("acom.backend")
frontend_logger = logging.getLogger("acom.frontend")

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


def _on_frame(frame: protocol.Frame) -> None:
    """Dispatches a decoded frame onto AmpState and schedules a WebSocket
    broadcast. Runs synchronously from SerialLink's asyncio read-loop task
    (same event loop, not a separate thread), so asyncio.create_task here
    is safe."""
    try:
        handled = True
        if frame.address == 0x2F:
            amp_state.update_telemetry(protocol.parse_telemetry(frame, amp_state.profile))
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


def _on_comms_change(ok: bool) -> None:
    if ok:
        amp_state.mark_comms_ok()
    else:
        amp_state.mark_comms_lost()
        backend_logger.warning("Lost comms with amplifier (no frames received recently)")
    _spawn(ws_manager.broadcast(amp_state.to_dict()))


def _require_serial_link() -> SerialLink:
    if serial_link is None:
        raise HTTPException(503, "serial link not yet started")
    return serial_link


@app.on_event("startup")
async def on_startup() -> None:
    global serial_link, modbus_server
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
    if config.modbus.enabled:
        modbus_server = ModbusServer(
            host=config.modbus.host, port=config.modbus.port, amp_state=amp_state, serial_link=serial_link
        )
        await modbus_server.start()


@app.on_event("shutdown")
async def on_shutdown() -> None:
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
    mode = protocol.MODE_OPR_RX if req.on else protocol.MODE_OFF
    return await _send_pulse_command(link, protocol.build_mode_change(mode))


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


# =============================================================================
# Setup Wizard: serial port enumeration + live reconnect
# =============================================================================


@app.get("/api/serial-ports")
async def get_serial_ports() -> dict:
    return {"ports": list_serial_ports(), "current": config.amp.serial_device}


# =============================================================================
# Setup Wizard: amp/tuner model + serial port (live, no restart)
# =============================================================================


class ConfigRequest(BaseModel):
    # Both Optional with no default so `exclude_unset` can distinguish
    # "field omitted - don't touch it" from "field explicitly sent (even
    # null) - apply it" - see set_config().
    amp_model: Optional[str] = None
    tuner_model: Optional[str] = None
    serial_device: Optional[str] = None
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
        "comms_ok": amp_state.comms_ok,
        "available_amp_models": sorted(AMP_PROFILES),
        "available_tuner_models": list(TUNER_MODELS),
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

    try:
        validate_config(new_amp_model, new_tuner_model)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

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

    config.amp.model = new_amp_model
    config.tuner.model = new_tuner_model
    if device_changed:
        config.amp.serial_device = new_serial_device
    amp_state.profile = get_amp_profile(new_amp_model)
    amp_state.tuner_model = new_tuner_model
    config.save()
    backend_logger.info(
        "Config updated live: amp=%s tuner=%s serial_device=%s", new_amp_model, new_tuner_model, config.amp.serial_device
    )
    return _config_response()


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

    names_result = await link.send_command(protocol.build_set_antenna_names(names))
    if not names_result.ok:
        raise HTTPException(502, f"amplifier did not confirm antenna names (param=0x{names_result.param:04X})")
    assignments_result = await link.send_command(protocol.build_set_antenna_assignments(ordered))
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
# Buzzer, frontend error log, destructive (web-UI-only) commands
# =============================================================================


class BuzzerRequest(BaseModel):
    signal: int = protocol.BUZZER_PULSE
    volume: int = 50


@app.post("/api/buzzer")
async def test_buzzer(req: BuzzerRequest) -> dict:
    link = _require_serial_link()
    result = await link.send_command(protocol.build_buzzer(req.signal, req.volume))
    return _require_confirmed(result)


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
    await link.enqueue(protocol.build_request_atu_tuning_record(req.antenna_number, req.band_number, req.record_number))
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
    backend_logger.warning(
        "Deleting segment tuning: antenna=%d band=%d record=%d",
        req.antenna_number, req.band_number, req.record_number,
    )
    result = await link.send_command(
        protocol.build_delete_segment_tuning(req.antenna_number, req.band_number, req.record_number)
    )
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
    backend_logger.warning("Destructive ATU memory erase requested: %s (antenna=%s)", req.selection, req.antenna_number)
    result = await link.send_command(protocol.build_atu_erase_memory(value, req.antenna_number or 0))
    return _require_confirmed(result)


# Mounted last so it doesn't shadow the /api and /ws routes registered above.
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
