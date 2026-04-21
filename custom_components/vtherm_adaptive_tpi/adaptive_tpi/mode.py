"""HVAC mode helpers for Adaptive TPI."""

from __future__ import annotations


def hvac_mode_sign(hvac_mode) -> int:
    """Return +1 for HEAT (or unrecognized active modes), -1 for COOL, 0 for OFF/sleep/None."""
    if hvac_mode is None:
        return 0
    mode_str = str(hvac_mode).lower()
    if mode_str.endswith(("off", "sleep")):
        return 0
    if "cool" in mode_str:
        return -1
    return 1
