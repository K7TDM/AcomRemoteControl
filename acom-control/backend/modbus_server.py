"""pymodbus async TCP server exposing amp telemetry/commands to 3rd-party
clients (e.g. QSO Director) - see PI_CONTROL_PLAN.md step 5.

Deliberately limited to normal operating commands: telemetry reads and
band/antenna cycling. Mode changes, OPR/TX, and all destructive commands
(erase/factory-reset) stay web-UI-only (see app.py) and are never
reachable here.

Written against and verified (imports + setValues/getValues round-trip)
against pymodbus==3.6.9's classic datastore API (ModbusServerContext/
ModbusSlaveContext/ModbusSequentialDataBlock from pymodbus.datastore,
StartAsyncTcpServer from pymodbus.server). pymodbus 3.7+ deprecated this
entire API in favor of a new SimData/SimDevice model (removal planned for
4.0) - requirements.txt pins to 3.6.9 deliberately; don't upgrade pymodbus
without rewriting this file against the new API.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext, ModbusSlaveContext
from pymodbus.server import StartAsyncTcpServer

from . import protocol
from .amp_state import AmpState
from .serial_link import SerialLink

logger = logging.getLogger("acom.backend.modbus")

# Holding registers (function code 3, read-only telemetry mirror)
REG_FORWARD_POWER_W = 0
REG_REFLECTED_POWER_W = 1
REG_SWR_X100 = 2
REG_HV1_X10V = 3
REG_CARRIER_FREQ_KHZ = 4
REG_AMP_MODE = 5
REG_ALARM_CODE = 6
HOLDING_REGISTER_COUNT = 16

# Coils (function code 1/5): 0-1 reflect power/OPR state (read-only from the
# client's perspective in practice), 2-4 are write-only momentary pulses
# mirroring the 0x09 band/antenna-cycle command.
COIL_POWER_ON = 0
COIL_OPR_RX = 1
COIL_NEXT_BAND = 2
COIL_PREV_BAND = 3
COIL_NEXT_ANTENNA = 4
COIL_COUNT = 16

TELEMETRY_POLL_INTERVAL_S = 0.5  # fine for a dashboard-style register mirror
COIL_POLL_INTERVAL_S = 0.1  # tighter so rapid repeated client pulses aren't coalesced


class ModbusServer:
    def __init__(self, host: str, port: int, amp_state: AmpState, serial_link: SerialLink) -> None:
        self.host = host
        self.port = port
        self.amp_state = amp_state
        self.serial_link = serial_link
        self._context: Optional[ModbusServerContext] = None
        self._slave: Optional[ModbusSlaveContext] = None
        self._server_task: Optional[asyncio.Task] = None
        self._telemetry_poll_task: Optional[asyncio.Task] = None
        self._coil_poll_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        holding = ModbusSequentialDataBlock(0, [0] * HOLDING_REGISTER_COUNT)
        coils = ModbusSequentialDataBlock(0, [0] * COIL_COUNT)
        self._slave = ModbusSlaveContext(hr=holding, co=coils)
        self._context = ModbusServerContext(slaves=self._slave, single=True)
        logger.info("Starting Modbus TCP server on %s:%d", self.host, self.port)
        self._server_task = asyncio.create_task(
            StartAsyncTcpServer(context=self._context, address=(self.host, self.port)),
            name="modbus-server",
        )
        self._telemetry_poll_task = asyncio.create_task(self._telemetry_poll_loop(), name="modbus-telemetry-poll")
        self._coil_poll_task = asyncio.create_task(self._coil_poll_loop(), name="modbus-coil-poll")

    async def stop(self) -> None:
        for task in (self._telemetry_poll_task, self._coil_poll_task, self._server_task):
            if task is not None:
                task.cancel()

    async def _telemetry_poll_loop(self) -> None:
        """Mirrors AmpState into the holding registers. Kept on its own,
        slower interval - separate from coil-pulse edge detection below,
        which needs to run much more often or it'll coalesce quick repeated
        client pulses into a single detected edge."""
        while True:
            await asyncio.sleep(TELEMETRY_POLL_INTERVAL_S)
            if self._slave is None:
                continue

            telemetry = self.amp_state.telemetry
            if telemetry is not None:
                self._slave.setValues(3, REG_FORWARD_POWER_W, [int(telemetry.forward_power_w)])
                self._slave.setValues(3, REG_REFLECTED_POWER_W, [int(telemetry.reflected_power_w)])
                self._slave.setValues(3, REG_SWR_X100, [int(telemetry.swr * 100)])
                self._slave.setValues(3, REG_HV1_X10V, [int(telemetry.hv1_v * 10)])
                self._slave.setValues(3, REG_CARRIER_FREQ_KHZ, [int(telemetry.carrier_freq_khz)])
                self._slave.setValues(3, REG_AMP_MODE, [telemetry.mode_byte])
                self._slave.setValues(3, REG_ALARM_CODE, [telemetry.alarm_code])
                self._slave.setValues(1, COIL_POWER_ON, [telemetry.mode_byte != protocol.MODE_OFF])
                self._slave.setValues(1, COIL_OPR_RX, [(telemetry.mode_byte >> 4) in (0x6, 0x7)])

    async def _coil_poll_loop(self) -> None:
        """Watches the write-only pulse coils, since pymodbus's datastore
        doesn't call back into our code on a client write. Polls much
        faster than the telemetry mirror above so two quick pulses from a
        3rd-party client (e.g. two antenna cycles within 500ms) aren't
        collapsed into one detected edge."""
        last_coils = [0] * COIL_COUNT
        while True:
            await asyncio.sleep(COIL_POLL_INTERVAL_S)
            if self._slave is None:
                continue

            coils = self._slave.getValues(1, 0, count=COIL_COUNT)
            for idx in (COIL_NEXT_BAND, COIL_PREV_BAND, COIL_NEXT_ANTENNA):
                if coils[idx] and not last_coils[idx]:
                    await self._handle_pulse(idx)
            # momentary pulses - clear the write-only coils back to 0 after acting
            for idx in (COIL_NEXT_BAND, COIL_PREV_BAND, COIL_NEXT_ANTENNA):
                coils[idx] = False
            self._slave.setValues(5, 0, coils)
            last_coils = coils

    async def _handle_pulse(self, coil_idx: int) -> None:
        if coil_idx == COIL_NEXT_BAND:
            logger.info("Modbus: next-band pulse")
            await self.serial_link.enqueue(protocol.build_band_change(protocol.BAND_NEXT))
        elif coil_idx == COIL_PREV_BAND:
            logger.info("Modbus: prev-band pulse")
            await self.serial_link.enqueue(protocol.build_band_change(protocol.BAND_PREV))
        elif coil_idx == COIL_NEXT_ANTENNA:
            logger.info("Modbus: next-antenna pulse")
            await self.serial_link.enqueue(protocol.build_antenna_next())
