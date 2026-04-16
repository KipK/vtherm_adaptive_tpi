"""Controller primitives for Adaptive TPI."""

from __future__ import annotations

from .supervisor import PHASE_A, PHASE_B, PHASE_C, PHASE_D

A_MIN_PROJ = 1e-3
C_D = 2.0
LAMBDA_CL = 0.90

KINT_MIN = 0.05
KINT_MAX = 1.2
KEXT_MIN = 0.0
KEXT_MAX = 0.3

PHASE_GAIN_RATE_LIMITS: dict[str, tuple[float, float]] = {
    PHASE_A: (0.0, 0.0),
    PHASE_B: (0.01, 0.002),
    PHASE_C: (0.03, 0.005),
    PHASE_D: (0.01, 0.002),
}


def _is_off_mode(hvac_mode) -> bool:
    """Return True when the HVAC mode should disable heating."""
    if hvac_mode is None:
        return True
    return str(hvac_mode).lower().endswith(("off", "sleep"))


def _clamp(value: float, lower: float, upper: float) -> float:
    """Clamp a value to an inclusive range."""
    return min(max(value, lower), upper)


def _saturate_delta(delta: float, max_delta: float) -> float:
    """Clamp a delta symmetrically around zero."""
    return _clamp(delta, -max_delta, max_delta)


def compute_gain_targets(
    *,
    a_hat: float,
    b_hat: float,
    nd_hat: float,
) -> tuple[float, float]:
    """Compute the structural gain targets from the current estimates."""
    a_proj = max(a_hat, A_MIN_PROJ)
    k_ext_target = b_hat / a_proj
    k_int_nom = max(0.0, 1.0 - LAMBDA_CL - b_hat) / a_proj
    gamma_d = 1.0 / (1.0 + C_D * max(nd_hat, 0.0) * max(b_hat, 0.0))
    k_int_target = gamma_d * k_int_nom
    return k_int_target, k_ext_target


def project_gains(
    *,
    phase: str,
    k_int: float,
    k_ext: float,
    a_hat: float,
    b_hat: float,
    nd_hat: float,
) -> tuple[float, float]:
    """Project the controller gains toward their structural targets."""
    if phase not in PHASE_GAIN_RATE_LIMITS:
        return k_int, k_ext

    delta_kint_max, delta_kext_max = PHASE_GAIN_RATE_LIMITS[phase]
    k_int_target, k_ext_target = compute_gain_targets(
        a_hat=a_hat,
        b_hat=b_hat,
        nd_hat=nd_hat,
    )

    next_k_int = k_int + _saturate_delta(k_int_target - k_int, delta_kint_max)
    next_k_ext = k_ext + _saturate_delta(k_ext_target - k_ext, delta_kext_max)

    return (
        _clamp(next_k_int, KINT_MIN, KINT_MAX),
        _clamp(next_k_ext, KEXT_MIN, KEXT_MAX),
    )


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
    """Compute the nominal clamped heating command."""
    if _is_off_mode(hvac_mode):
        return 0.0

    e_in = target_temp - current_temp
    e_out = target_temp - outdoor_temp if outdoor_temp is not None else 0.0
    command = k_int * e_in + k_ext * e_out
    command = _clamp(command, 0.0, 1.0)

    if max_on_percent is not None:
        command = min(command, max_on_percent)

    return command
