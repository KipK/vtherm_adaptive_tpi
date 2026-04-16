"""Controller primitives for Adaptive TPI."""

from __future__ import annotations


def _is_off_mode(hvac_mode) -> bool:
    """Return True when the HVAC mode should disable heating."""
    if hvac_mode is None:
        return True
    return str(hvac_mode).lower().endswith(("off", "sleep"))


def compute_on_percent(
    *,
    hvac_mode,
    target_temp: float,
    current_temp: float,
    outdoor_temp: float | None,
    k_int: float,
    k_ext: float,
    max_on_percent: float | None,
) -> float:
    """Compute a clamped placeholder TPI command."""
    if _is_off_mode(hvac_mode):
        return 0.0

    delta_in = target_temp - current_temp
    delta_out = target_temp - outdoor_temp if outdoor_temp is not None else 0.0
    command = k_int * delta_in + k_ext * delta_out
    command = max(0.0, min(1.0, command))

    if max_on_percent is not None:
        command = min(command, max_on_percent)

    return command

