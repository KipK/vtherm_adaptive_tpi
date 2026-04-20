"""Controller primitives for Adaptive TPI."""

from __future__ import annotations

import math

from ..const import DEFAULT_KEXT, DEFAULT_KINT
from .supervisor import PHASE_A, PHASE_B, PHASE_C, PHASE_D, PHASE_STARTUP

A_MIN_PROJ = 1e-3
TAU_CL_MIN_CYCLES = 3.0
K_THETA = 2.0

KINT_MIN = 0.05
KINT_MAX = 1.2
KEXT_MIN = 0.0
KEXT_MAX = 0.3

DEADTIME_CONFIDENCE_GATE_SOFT = 0.3
DEADTIME_CONFIDENCE_GATE_HARD = 0.6
ESTIMATOR_CONFIDENCE_GATE = 0.2

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


def _closed_loop_pole(nd_hat: float, tau_cl_min: float = TAU_CL_MIN_CYCLES) -> float:
    """Return the discrete closed-loop pole derived from the deadtime estimate."""
    tau_cl = max(tau_cl_min, K_THETA * max(nd_hat, 0.0))
    return math.exp(-1.0 / tau_cl)


def compute_gain_targets(
    *,
    a_hat: float,
    b_hat: float,
    nd_hat: float,
    tau_cl_min: float = TAU_CL_MIN_CYCLES,
) -> tuple[float, float]:
    """Compute the structural gain targets from the current estimates."""
    a_proj = max(a_hat, A_MIN_PROJ)
    lambda_cl = _closed_loop_pole(nd_hat, tau_cl_min=tau_cl_min)
    k_ext_target = b_hat / a_proj
    k_int_target = max(0.0, 1.0 - lambda_cl - b_hat) / a_proj
    return k_int_target, k_ext_target


def _confidence_weight(value: float, lower: float, upper: float) -> float:
    """Return a normalized confidence weight in [0, 1]."""
    if upper <= lower:
        return 0.0
    return _clamp((value - lower) / (upper - lower), 0.0, 1.0)


def _structural_gain_weight(
    *,
    phase: str,
    c_nd: float,
    c_a: float,
    c_b: float,
) -> float:
    """Return how much of the structural model may influence the gains."""
    if phase in (PHASE_STARTUP, PHASE_A):
        return 0.0

    if phase == PHASE_B:
        if c_nd < DEADTIME_CONFIDENCE_GATE_SOFT:
            return 0.0
        if min(c_a, c_b) < ESTIMATOR_CONFIDENCE_GATE:
            return 0.0
        return 0.25 * min(
            _confidence_weight(c_nd, DEADTIME_CONFIDENCE_GATE_SOFT, DEADTIME_CONFIDENCE_GATE_HARD),
            _confidence_weight(min(c_a, c_b), ESTIMATOR_CONFIDENCE_GATE, 0.5),
        )

    return min(
        _confidence_weight(c_nd, DEADTIME_CONFIDENCE_GATE_SOFT, DEADTIME_CONFIDENCE_GATE_HARD),
        _confidence_weight(min(c_a, c_b), ESTIMATOR_CONFIDENCE_GATE, 0.5),
    )


def project_gains(
    *,
    phase: str,
    k_int: float,
    k_ext: float,
    a_hat: float,
    b_hat: float,
    nd_hat: float,
    c_nd: float = 0.0,
    c_a: float = 0.0,
    c_b: float = 0.0,
    tau_cl_min: float = TAU_CL_MIN_CYCLES,
) -> tuple[float, float]:
    """Project the controller gains toward their structural targets."""
    if phase not in PHASE_GAIN_RATE_LIMITS:
        if phase == PHASE_STARTUP:
            return DEFAULT_KINT, DEFAULT_KEXT
        return k_int, k_ext

    structural_weight = _structural_gain_weight(
        phase=phase,
        c_nd=c_nd,
        c_a=c_a,
        c_b=c_b,
    )
    if structural_weight <= 0.0:
        return k_int, k_ext

    delta_kint_max, delta_kext_max = PHASE_GAIN_RATE_LIMITS[phase]
    k_int_target, k_ext_target = compute_gain_targets(
        a_hat=a_hat,
        b_hat=b_hat,
        nd_hat=nd_hat,
        tau_cl_min=tau_cl_min,
    )
    k_int_target = ((1.0 - structural_weight) * DEFAULT_KINT) + (structural_weight * k_int_target)
    k_ext_target = ((1.0 - structural_weight) * DEFAULT_KEXT) + (structural_weight * k_ext_target)

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
