"""Loads/validates config.json and persists live Setup-screen changes back
to disk (see PI_CONTROL_PLAN.md "Setup Wizard & Antenna Setup" - config
changes are fully live, no service restart, but still saved for next boot).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .amp_profiles import AMP_PROFILES, TUNER_MODELS

logger = logging.getLogger("acom.backend.config")

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


def validate_config(amp_model: str, tuner_model: Optional[str]) -> None:
    if amp_model not in AMP_PROFILES:
        raise ValueError(
            f"amp.model={amp_model!r} is not a known profile; "
            f"expected one of {sorted(AMP_PROFILES)}"
        )
    if tuner_model is not None and tuner_model not in TUNER_MODELS:
        raise ValueError(
            f"tuner.model={tuner_model!r} is not recognized; "
            f"expected one of {TUNER_MODELS} or null"
        )


@dataclass
class AmpConfig:
    model: str
    serial_device: str


@dataclass
class TunerConfig:
    model: Optional[str] = None


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 8000


@dataclass
class ModbusConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 5020


@dataclass
class LoggingConfig:
    level: str = "INFO"
    # Set by POST /api/logs/clear (Setup screen's "Clear Log" button) - a
    # journalctl `--since`-compatible local-time string. Doesn't touch
    # journald itself (the acom-control user only has read access via
    # systemd-journal, see setup/acom-control.service), just moves the
    # start of what GET /api/logs exports forward so old entries stop
    # showing up in downloads. None means "no marker set - export the
    # usual tail of recent lines instead."
    log_marker: Optional[str] = None


@dataclass
class UpdateConfig:
    # Update screen's "beta updates" toggle - when true, self-update tracks
    # the tip of origin/main instead of the latest vX.Y.Z tag (see
    # setup/acom-update-apply.sh's check-beta/beta modes).
    beta: bool = False


@dataclass
class AppConfig:
    amp: AmpConfig
    tuner: TunerConfig
    web: WebConfig
    modbus: ModbusConfig
    logging: LoggingConfig
    path: Path = field(repr=False)
    update: UpdateConfig = field(default_factory=UpdateConfig)

    def save(self) -> None:
        payload = {
            "amp": asdict(self.amp),
            "tuner": asdict(self.tuner),
            "web": asdict(self.web),
            "modbus": asdict(self.modbus),
            "logging": asdict(self.logging),
            "update": asdict(self.update),
        }
        self.path.write_text(json.dumps(payload, indent=2) + "\n")
        logger.info("Saved config to %s", self.path)


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    if not path.exists():
        raise FileNotFoundError(f"config.json not found at {path}")
    data = json.loads(path.read_text())

    amp = AmpConfig(**data["amp"])
    tuner = TunerConfig(**data.get("tuner", {}))
    web = WebConfig(**data.get("web", {}))
    modbus = ModbusConfig(**data.get("modbus", {}))
    logging_cfg = LoggingConfig(**data.get("logging", {}))
    update = UpdateConfig(**data.get("update", {}))

    validate_config(amp.model, tuner.model)

    if not os.path.exists(amp.serial_device):
        # Don't fail startup over this (unlike the hard FileNotFoundError
        # this used to raise) - the device may just be unplugged, or the
        # user is about to pick a different one from the Setup screen's
        # serial-port selector. The backend starts with comms down instead;
        # see app.py's on_startup for how SerialLink.start() failing is
        # handled.
        logger.warning(
            "amp.serial_device=%r does not exist at startup (USB-RS232 "
            "adapter unplugged, or the udev rule in setup/99-acom-amp.rules "
            "isn't installed) - starting anyway with comms down. Pick a "
            "working port from the Setup screen, or see README.md.",
            amp.serial_device,
        )

    return AppConfig(amp=amp, tuner=tuner, web=web, modbus=modbus, logging=logging_cfg, path=path, update=update)
