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


@dataclass
class AppConfig:
    amp: AmpConfig
    tuner: TunerConfig
    web: WebConfig
    modbus: ModbusConfig
    logging: LoggingConfig
    path: Path = field(repr=False)

    def save(self) -> None:
        payload = {
            "amp": asdict(self.amp),
            "tuner": asdict(self.tuner),
            "web": asdict(self.web),
            "modbus": asdict(self.modbus),
            "logging": asdict(self.logging),
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

    validate_config(amp.model, tuner.model)

    if not os.path.exists(amp.serial_device):
        # Fail loudly rather than silently falling back, per
        # PI_CONTROL_PLAN.md step 0 - a missing serial device almost always
        # means the USB-RS232 adapter isn't plugged in yet, or the udev
        # rule in setup/99-acom-amp.rules hasn't been installed.
        raise FileNotFoundError(
            f"amp.serial_device={amp.serial_device!r} does not exist. "
            "Check the USB-RS232 adapter is plugged in and the udev rule in "
            "setup/99-acom-amp.rules is installed (see README.md)."
        )

    return AppConfig(amp=amp, tuner=tuner, web=web, modbus=modbus, logging=logging_cfg, path=path)
