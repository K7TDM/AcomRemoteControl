"""Shared in-memory amplifier state - the single source of truth that
app.py (REST + WebSocket) and modbus_server.py both read from.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from . import protocol
from .amp_profiles import AmpProfile


@dataclass
class AmpState:
    profile: AmpProfile
    tuner_model: Optional[str] = None

    comms_ok: bool = False
    last_update_ts: float = 0.0

    telemetry: Optional[protocol.AmpTelemetry] = None
    atu_identity: Optional[protocol.AtuIdentity] = None
    antenna_band_status: Optional[protocol.AntennaBandStatus] = None
    error_flags: Optional[protocol.ErrorFlags] = None
    atu_segment_tuning: Optional[protocol.AtuSegmentTuning] = None
    atu_service_info: Optional[protocol.AtuServiceInfo] = None
    atu_measure: Optional[protocol.AtuMeasureTelemetry] = None
    last_command_result: Optional[protocol.CommandAck] = None

    antenna_config: list[protocol.AntennaSlot] = field(
        default_factory=lambda: [protocol.AntennaSlot(number=i) for i in range(1, 5)]
    )

    def mark_comms_ok(self) -> None:
        self.comms_ok = True
        self.last_update_ts = time.time()

    def mark_comms_lost(self) -> None:
        self.comms_ok = False

    def update_telemetry(self, telemetry: protocol.AmpTelemetry) -> None:
        self.telemetry = telemetry
        self.mark_comms_ok()

    def update_atu_identity(self, identity: protocol.AtuIdentity) -> None:
        self.atu_identity = identity
        self.mark_comms_ok()

    def update_antenna_band_status(self, status: protocol.AntennaBandStatus) -> None:
        self.antenna_band_status = status
        self.mark_comms_ok()

    def update_error_flags(self, flags: protocol.ErrorFlags) -> None:
        self.error_flags = flags
        self.mark_comms_ok()

    def update_atu_segment_tuning(self, tuning: protocol.AtuSegmentTuning) -> None:
        self.atu_segment_tuning = tuning
        self.mark_comms_ok()

    def update_atu_service_info(self, info: protocol.AtuServiceInfo) -> None:
        self.atu_service_info = info
        self.mark_comms_ok()

    def update_atu_measure(self, measure: protocol.AtuMeasureTelemetry) -> None:
        self.atu_measure = measure
        self.mark_comms_ok()

    def update_last_command_result(self, result: protocol.CommandAck) -> None:
        self.last_command_result = result
        self.mark_comms_ok()

    def update_antenna_assignments(self, slots: list[protocol.AntennaSlot]) -> None:
        """Merges type/band_mask from message 0x18 into the existing
        name-bearing slots (names arrive separately via 0x1B)."""
        by_number = {s.number: s for s in self.antenna_config}
        for slot in slots:
            existing = by_number.get(slot.number)
            if existing is not None:
                existing.type = slot.type
                existing.band_mask = slot.band_mask
            else:
                self.antenna_config.append(slot)
        self.antenna_config.sort(key=lambda s: s.number)
        self.mark_comms_ok()

    def update_antenna_names(self, names: list[str]) -> None:
        ordered = sorted(self.antenna_config, key=lambda s: s.number)
        for slot, name in zip(ordered, names):
            slot.name = name
        self.mark_comms_ok()

    def to_dict(self) -> dict:
        return {
            "comms_ok": self.comms_ok,
            "last_update_ts": self.last_update_ts,
            "amp_model": self.profile.model,
            "max_power_w": self.profile.max_power_w,
            "tuner_model": self.tuner_model,
            "telemetry": _telemetry_to_dict(self.telemetry),
            "atu_identity": _atu_identity_to_dict(self.atu_identity),
            "antenna_band_status": _band_status_to_dict(self.antenna_band_status),
            "error_flags": _error_flags_to_dict(self.error_flags),
            "antenna_config": [slot.to_dict() for slot in self.antenna_config],
            "atu_segment_tuning": _atu_segment_tuning_to_dict(self.atu_segment_tuning),
            "atu_service_info": _atu_service_info_to_dict(self.atu_service_info),
            "atu_measure": _atu_measure_to_dict(self.atu_measure),
            "last_command_result": _command_ack_to_dict(self.last_command_result),
        }


def _telemetry_to_dict(t: Optional[protocol.AmpTelemetry]) -> Optional[dict]:
    if t is None:
        return None
    return {
        "mode_byte": t.mode_byte,
        "mode_name": t.mode_name,
        "p_dc1_w": t.p_dc1_w,
        "p_dc2_w": t.p_dc2_w,
        "system_clock_s": t.system_clock_s,
        "temp_pam1_c": t.temp_pam1_c,
        "temp_pam2_c": t.temp_pam2_c,
        "input_power_w": t.input_power_w,
        "forward_power_w": t.forward_power_w,
        "reflected_power_w": t.reflected_power_w,
        "swr": t.swr,
        "pam1_dissipation_w": t.pam1_dissipation_w,
        "pam2_dissipation_w": t.pam2_dissipation_w,
        "vcc5_mv": t.vcc5_mv,
        "vcc26_v": t.vcc26_v,
        "hv1_v": t.hv1_v,
        "hv2_v": t.hv2_v,
        "id1_ma": t.id1_ma,
        "id2_ma": t.id2_ma,
        "carrier_freq_khz": t.carrier_freq_khz,
        "bias_1a_mv": t.bias_1a_mv,
        "bias_1b_mv": t.bias_1b_mv,
        "alarm_code": t.alarm_code,
        "alarm_param": t.alarm_param,
        "fan_speed": t.fan_speed,
        "active_lpf_channel": t.active_lpf_channel,
        "default_antenna_selected": t.default_antenna_selected,
        "atu_assigned": t.atu_assigned,
        "hide_segments_scale": t.hide_segments_scale,
        "last_fault_is_hard": t.last_fault_is_hard,
    }


def _atu_identity_to_dict(a: Optional[protocol.AtuIdentity]) -> Optional[dict]:
    if a is None:
        return None
    return {
        "type_code": a.type_code,
        "type_name": a.type_name,
        "present": a.present,
        "hw_version": a.hw_version,
        "hw_subversion": a.hw_subversion,
        "fw_version": a.fw_version,
        "fw_subversion": a.fw_subversion,
        "serial_number": a.serial_number,
    }


def _band_status_to_dict(s: Optional[protocol.AntennaBandStatus]) -> Optional[dict]:
    if s is None:
        return None
    return {
        "antenna_number": s.antenna_number,
        "antenna_type": s.antenna_type,
        "is_default_antenna": s.is_default_antenna,
        "band_number": s.band_number,
        "segment_count": s.segment_count,
        "current_segment": s.current_segment,
        "low_border_khz": s.low_border_khz,
        "high_border_khz": s.high_border_khz,
        "segment_status_raw": list(s.segment_status_raw),
        "atu_label": s.atu_label,
        "atu_label_name": s.atu_label_name,
    }


def _error_flags_to_dict(e: Optional[protocol.ErrorFlags]) -> Optional[dict]:
    if e is None:
        return None
    active = e.active_bits()
    return {
        "active_codes": [f"0x{c:02X}" for c in active],
        "active_names": [protocol.FAULT_CODE_NAMES.get(c, f"Unknown fault 0x{c:02X}") for c in active],
    }


def _atu_segment_tuning_to_dict(t: Optional[protocol.AtuSegmentTuning]) -> Optional[dict]:
    if t is None:
        return None
    return {
        "band_number": t.band_number,
        "low_freq_khz": t.low_freq_khz,
        "high_freq_khz": t.high_freq_khz,
        "segment_count": t.segment_count,
        "current_segment": t.current_segment,
        "antenna_number": t.antenna_number,
        "antenna_type": t.antenna_type,
        "segment_low_freq_khz": t.segment_low_freq_khz,
        "segment_high_freq_khz": t.segment_high_freq_khz,
        "tuning_ct_pct": t.tuning_ct_pct,
        "tuning_l_pct": t.tuning_l_pct,
        "tuning_ca_pct": t.tuning_ca_pct,
        "tuning_age_hours": t.tuning_age_hours,
    }


def _atu_service_info_to_dict(i: Optional[protocol.AtuServiceInfo]) -> Optional[dict]:
    if i is None:
        return None
    return {
        "model_code": i.model_code,
        "hw_version": i.hw_version,
        "hw_subversion": i.hw_subversion,
        "fw_version": i.fw_version,
        "fw_subversion": i.fw_subversion,
        "working_hours": i.working_hours,
        "tune_ct_pct": i.tune_ct_pct,
        "tune_l_pct": i.tune_l_pct,
        "tune_ca_pct": i.tune_ca_pct,
        "serial_number": i.serial_number,
    }


def _atu_measure_to_dict(m: Optional[protocol.AtuMeasureTelemetry]) -> Optional[dict]:
    if m is None:
        return None
    return {
        "temp1_c": m.temp1_c,
        "temp2_c": m.temp2_c,
        "input_power_w": m.input_power_w,
        "forward_power_w": m.forward_power_w,
        "reflected_power_w": m.reflected_power_w,
        "atu_ps_mv": m.atu_ps_mv,
        "antenna_voltage_v": m.antenna_voltage_v,
        "antenna_current_a": m.antenna_current_a,
        "antenna_fwd_power_w": m.antenna_fwd_power_w,
        "antenna_refl_power_w": m.antenna_refl_power_w,
        "atu_swr": m.atu_swr,
        "atu_temp_c": m.atu_temp_c,
        "atu_working_hours": m.atu_working_hours,
        "sys_status": m.sys_status,
    }


def _command_ack_to_dict(a: Optional[protocol.CommandAck]) -> Optional[dict]:
    if a is None:
        return None
    return {"ok": a.ok, "param": a.param}
