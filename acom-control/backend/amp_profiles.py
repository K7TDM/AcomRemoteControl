"""Per-amplifier-model capability table.

See PI_CONTROL_PLAN.md "Model abstraction" - this is the extension point
for adding real 600S/2000S field-level support later without touching
protocol.py's framing/checksum code.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AmpProfile:
    model: str
    has_pam2: bool
    max_power_w: int


AMP_PROFILES: dict[str, AmpProfile] = {
    "600S": AmpProfile(model="600S", has_pam2=False, max_power_w=600),
    "1200S": AmpProfile(model="1200S", has_pam2=False, max_power_w=1200),
    "2000S": AmpProfile(model="2000S", has_pam2=True, max_power_w=2000),
}

# Tuner/switch model is informational only (status display, see CLAUDE.md's
# tuner-command protocol gap) - all three expose the same 4-antenna-slot
# name/assignment tables over RS-232.
TUNER_MODELS: tuple[str, ...] = ("04AT", "06AT", "20AT")


def get_amp_profile(model: str) -> AmpProfile:
    try:
        return AMP_PROFILES[model]
    except KeyError as exc:
        raise ValueError(
            f"Unknown amp model {model!r}; expected one of {sorted(AMP_PROFILES)}"
        ) from exc
