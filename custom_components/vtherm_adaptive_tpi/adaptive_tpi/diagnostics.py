"""Diagnostics helpers for Adaptive TPI."""

from __future__ import annotations

from .state import AdaptiveTPIState

_PUBLIC_PHASE_NAMES = {
    "startup": "startup",
    "phase_a": "deadtime_learning",
    "phase_b": "cooling_learning",
    "phase_c": "heating_learning",
    "phase_d": "stabilized",
}

_PUBLIC_STARTUP_STAGE_NAMES = {
    "idle": "idle",
    "preheat_to_target": "heating_to_target",
    "cooldown_below_target": "cooling_below_target",
    "reheat_to_target": "reheating_to_target",
    "completed": "completed",
    "abandoned": "abandoned",
}

_PUBLIC_LEARNING_FAMILY_NAMES = {
    "a": "heating",
    "b": "cooling",
}


def _per_hour(value_per_cycle: float, cycle_min: float | None) -> float | None:
    """Convert one per-cycle value into a per-hour diagnostic when possible."""
    if cycle_min is None or cycle_min <= 0:
        return None
    return value_per_cycle * (60.0 / cycle_min)


def _deadtime_min(nd_hat: float, cycle_min: float | None) -> float | None:
    """Convert a deadtime expressed in cycles into minutes when possible."""
    if cycle_min is None or cycle_min <= 0:
        return None
    return nd_hat * cycle_min


def _tau_h(b_per_hour: float | None) -> float | None:
    """Return the thermal time constant in hours when the loss rate is positive."""
    if b_per_hour is None or b_per_hour <= 0:
        return None
    return 1.0 / b_per_hour


def _public_phase_name(phase: str | None) -> str | None:
    """Map one internal supervisor phase to a user-facing label."""
    if phase is None:
        return None
    return _PUBLIC_PHASE_NAMES.get(phase, phase)


def _public_startup_stage_name(stage: str | None) -> str | None:
    """Map one internal startup stage to a user-facing label."""
    if stage is None:
        return None
    return _PUBLIC_STARTUP_STAGE_NAMES.get(stage, stage)


def _public_learning_family_name(family: str | None) -> str | None:
    """Map one internal estimator family to a user-facing label."""
    if family is None:
        return None
    return _PUBLIC_LEARNING_FAMILY_NAMES.get(family, family)


def build_diagnostics(state: AdaptiveTPIState, debug_mode: bool) -> dict:
    """Build the stable diagnostics payload exposed by the algorithm."""
    cycle_min = state.cycle_min_at_last_accepted_cycle
    a_per_hour = _per_hour(state.a_hat, cycle_min)
    b_per_hour = _per_hour(state.b_hat, cycle_min)
    tau_h = _tau_h(b_per_hour)
    data = {
        "adaptive_phase": _public_phase_name(state.bootstrap_phase),
        "gain_indoor": state.k_int,
        "gain_outdoor": state.k_ext,
        "deadtime_cycles": state.nd_hat,
        "deadtime_minutes": _deadtime_min(state.nd_hat, cycle_min),
        "deadtime_confidence": state.c_nd,
        "heating_rate_per_hour": a_per_hour,
        "cooling_rate_per_hour": b_per_hour,
        "thermal_time_constant_hours": tau_h,
        "thermal_time_constant_minutes": (tau_h * 60.0) if tau_h is not None else None,
        "heating_rate_confidence": state.c_a,
        "cooling_rate_confidence": state.c_b,
        "cooling_rate_converged": state.b_converged,
        "heating_samples": state.a_samples_count,
        "cooling_samples": state.b_samples_count,
        "heating_learning_enabled": state.a_learning_enabled,
        "startup_sequence_active": state.startup_bootstrap_active,
        "startup_sequence_stage": _public_startup_stage_name(
            state.startup_bootstrap_stage
        ),
        "startup_sequence_attempt": state.startup_bootstrap_attempt,
        "startup_sequence_max_attempts": state.startup_bootstrap_max_attempts,
        "startup_sequence_target_temperature": state.startup_bootstrap_target_temp,
        "startup_sequence_cooling_temperature": state.startup_bootstrap_lower_target_temp,
        "startup_sequence_requested_power": state.startup_bootstrap_command_on_percent,
        "startup_sequence_completion_reason": state.startup_bootstrap_completion_reason,
        "last_learning_result": state.last_learning_attempt_reason,
        "last_learning_family": _public_learning_family_name(
            state.last_learning_attempt_regime
        ),
        "last_runtime_blocker": state.last_freeze_reason,
    }
    if debug_mode:
        data["debug"] = {
            "bootstrap_phase": state.bootstrap_phase,
            "phase": state.bootstrap_phase,
            "k_int": state.k_int,
            "k_ext": state.k_ext,
            "nd_hat": state.nd_hat,
            "nd_hat_cycles": state.nd_hat,
            "deadtime_min": _deadtime_min(state.nd_hat, cycle_min),
            "c_nd": state.c_nd,
            "a_hat": state.a_hat,
            "a_hat_per_hour": a_per_hour,
            "b_hat": state.b_hat,
            "b_hat_per_hour": b_per_hour,
            "tau_h": tau_h,
            "tau_min": (tau_h * 60.0) if tau_h is not None else None,
            "c_a": state.c_a,
            "c_b": state.c_b,
            "b_converged": state.b_converged,
            "i_a": state.i_a,
            "i_b": state.i_b,
            "a_samples_count": state.a_samples_count,
            "b_samples_count": state.b_samples_count,
            "a_last_reason": state.a_last_reason,
            "b_last_reason": state.b_last_reason,
            "last_learning_attempt_reason": state.last_learning_attempt_reason,
            "last_learning_attempt_regime": state.last_learning_attempt_regime,
            "a_learning_enabled": state.a_learning_enabled,
            "current_cycle_regime": state.current_cycle_regime,
            "learning_route_selected": state.learning_route_selected,
            "learning_route_block_reason": state.learning_route_block_reason,
            "deadtime_learning_blackout_active": state.deadtime_learning_blackout_active,
            "startup_bootstrap_active": state.startup_bootstrap_active,
            "startup_bootstrap_stage": state.startup_bootstrap_stage,
            "startup_bootstrap_attempt": state.startup_bootstrap_attempt,
            "startup_bootstrap_max_attempts": state.startup_bootstrap_max_attempts,
            "startup_bootstrap_target_temp": state.startup_bootstrap_target_temp,
            "startup_bootstrap_lower_target_temp": state.startup_bootstrap_lower_target_temp,
            "startup_bootstrap_command_on_percent": state.startup_bootstrap_command_on_percent,
            "startup_bootstrap_completion_reason": state.startup_bootstrap_completion_reason,
            "a_dispersion": state.a_dispersion,
            "b_dispersion": state.b_dispersion,
            "deadtime_identification_count": state.deadtime_identification_count,
            "deadtime_identification_qualities": state.deadtime_identification_qualities,
            "deadtime_b_proxy": state.deadtime_b_proxy,
            "b_crosscheck_error": state.b_crosscheck_error,
            "b_methods_consistent": state.b_methods_consistent,
            "last_freeze_reason": state.last_freeze_reason,
            "accepted_cycles_count": state.accepted_cycles_count,
            "hours_without_excitation": state.hours_without_excitation,
            "cycle_min_at_last_accepted_cycle": state.cycle_min_at_last_accepted_cycle,
            "cycle_started_calls_count": state.cycle_started_calls_count,
            "cycle_completed_calls_count": state.cycle_completed_calls_count,
            "last_cycle_started_at": state.last_cycle_started_at,
            "last_cycle_completed_at": state.last_cycle_completed_at,
            "last_cycle_classification": state.last_cycle_classification,
            "valid_cycles_count": state.valid_cycles_count,
            "informative_deadtime_cycles_count": state.informative_deadtime_cycles_count,
            "adaptive_cycles_since_phase_c": state.adaptive_cycles_since_phase_c,
            "deadtime_locked": state.deadtime_locked,
            "deadtime_pending_step": state.deadtime_pending_step,
            "deadtime_best_candidate": state.deadtime_best_candidate,
            "deadtime_second_best_candidate": state.deadtime_second_best_candidate,
            "calculated_on_percent": state.calculated_on_percent,
            "on_percent": state.on_percent,
        }
    return data
