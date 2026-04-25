"""Diagnostics helpers for Adaptive TPI."""

from __future__ import annotations

from .estimator import WINDOW_HISTORY
from .state import AdaptiveTPIState

_PUBLIC_PHASE_NAMES = {
    "startup": "startup",
    "phase_a": "deadtime_learning",
    "phase_b": "drift_learning",
    "phase_c": "control_learning",
    "phase_d": "stabilized",
}

_PUBLIC_STARTUP_STAGE_NAMES = {
    "idle": "idle",
    "preheat_to_target": "active_to_target",
    "cooldown_below_target": "passive_drift_phase",
    "reheat_to_target": "reactivation_to_target",
    "reheat_to_upper_target": "reactivation_to_upper_target",
    "cooldown_to_target": "return_to_target",
    "completed": "completed",
    "abandoned": "abandoned",
}

_PUBLIC_LEARNING_FAMILY_NAMES = {
    "a": "control",
    "b": "drift",
}


def _per_hour(value_per_cycle: float, cycle_min: float | None) -> float | None:
    """Convert one per-cycle value into a per-hour diagnostic when possible."""
    if cycle_min is None or cycle_min <= 0:
        return None
    return value_per_cycle * (60.0 / cycle_min)


def _deadtime_min(nd_hat: float | None, cycle_min: float | None) -> float | None:
    """Convert a deadtime expressed in cycles into minutes when possible."""
    if nd_hat is None:
        return None
    if cycle_min is None or cycle_min <= 0:
        return None
    return nd_hat * cycle_min


def _published_deadtime_minutes(state: AdaptiveTPIState) -> float | None:
    """Return the measured deadtime in minutes when available."""
    if state.deadtime_minutes is not None:
        return state.deadtime_minutes
    return _deadtime_min(state.nd_hat, state.cycle_min_at_last_accepted_cycle)


def _published_deadtime_on_cycles(state: AdaptiveTPIState) -> float | None:
    """Return the ON-transition deadtime while preserving legacy aliases."""
    if state.deadtime_on_cycles is not None:
        return state.deadtime_on_cycles
    return state.nd_hat


def _published_deadtime_on_minutes(state: AdaptiveTPIState) -> float | None:
    """Return the measured ON-transition deadtime in minutes when available."""
    if state.deadtime_on_minutes is not None:
        return state.deadtime_on_minutes
    return _published_deadtime_minutes(state)


def _published_deadtime_off_minutes(state: AdaptiveTPIState) -> float | None:
    """Return the measured OFF-transition deadtime in minutes when available."""
    if state.deadtime_off_minutes is not None:
        return state.deadtime_off_minutes
    return _deadtime_min(
        state.deadtime_off_cycles,
        state.cycle_min_at_last_accepted_cycle,
    )


def _tau_h(b_per_hour: float | None) -> float | None:
    """Return the thermal time constant in hours when the loss rate is positive."""
    if b_per_hour is None or b_per_hour <= 0:
        return None
    return 1.0 / b_per_hour


def _control_rate_converged(state: AdaptiveTPIState) -> bool:
    """Return True when the actuator authority estimate meets the phase-C confidence target."""
    return state.c_a >= 0.6


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
    control_converged = _control_rate_converged(state)
    data = {
        "adaptive_phase": _public_phase_name(state.bootstrap_phase),
        "gain_indoor": state.k_int,
        "gain_outdoor": state.k_ext,
        "actuator_mode": state.actuator_mode,
        "valve_curve_params": state.valve_curve_params,
        "valve_curve_learning_enabled": state.valve_curve_learning_enabled,
        "valve_curve_converged": state.valve_curve_converged,
        "valve_curve_observations_accepted": state.valve_curve_observations_accepted,
        "valve_curve_observations_rejected": state.valve_curve_observations_rejected,
        "valve_curve_last_reason": state.valve_curve_last_reason,
        "current_cycle_percent": state.committed_on_percent,
        "next_cycle_percent": state.requested_on_percent,
        "deadtime_cycles": state.nd_hat,
        "deadtime_confidence": state.c_nd,
        "deadtime_on_cycles": _published_deadtime_on_cycles(state),
        "deadtime_on_minutes": _published_deadtime_on_minutes(state),
        "deadtime_on_confidence": state.deadtime_on_confidence,
        "deadtime_on_locked": state.deadtime_on_locked,
        "deadtime_off_cycles": state.deadtime_off_cycles,
        "deadtime_off_minutes": _published_deadtime_off_minutes(state),
        "deadtime_off_confidence": state.deadtime_off_confidence,
        "deadtime_off_locked": state.deadtime_off_locked,
        "control_rate_per_hour": a_per_hour,
        "drift_rate_per_hour": b_per_hour,
        "thermal_time_constant_hours": tau_h,
        "control_rate_confidence": state.c_a,
        "drift_rate_confidence": state.c_b,
        "control_rate_converged": control_converged,
        "drift_rate_converged": state.b_converged,
        "control_samples": state.a_samples_count,
        "drift_samples": state.b_samples_count,
        "sample_window_size": WINDOW_HISTORY,
        "control_learning_enabled": state.a_learning_enabled,
        "startup_sequence_active": state.startup_bootstrap_active,
        "startup_sequence_stage": _public_startup_stage_name(
            state.startup_bootstrap_stage
        ),
        "startup_sequence_attempt": state.startup_bootstrap_attempt,
        "startup_sequence_max_attempts": state.startup_bootstrap_max_attempts,
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
            "deadtime_min": _published_deadtime_minutes(state),
            "c_nd": state.c_nd,
            "a_hat": state.a_hat,
            "a_hat_per_hour": a_per_hour,
            "b_hat": state.b_hat,
            "b_hat_per_hour": b_per_hour,
            "tau_h": tau_h,
            "tau_min": (tau_h * 60.0) if tau_h is not None else None,
            "c_a": state.c_a,
            "c_b": state.c_b,
            "control_rate_converged": control_converged,
            "b_converged": state.b_converged,
            "i_a": state.i_a,
            "i_b": state.i_b,
            "a_samples_count": state.a_samples_count,
            "b_samples_count": state.b_samples_count,
            "sample_window_size": WINDOW_HISTORY,
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
            "startup_bootstrap_upper_target_temp": state.startup_bootstrap_upper_target_temp,
            "startup_bootstrap_command_on_percent": state.startup_bootstrap_command_on_percent,
            "startup_bootstrap_completion_reason": state.startup_bootstrap_completion_reason,
            "startup_sequence_target_temperature": state.startup_bootstrap_target_temp,
            "startup_sequence_cooling_temperature": state.startup_bootstrap_lower_target_temp,
            "startup_sequence_heating_temperature": state.startup_bootstrap_upper_target_temp,
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
            "deadtime_on_cycles": _published_deadtime_on_cycles(state),
            "deadtime_on_minutes": _published_deadtime_on_minutes(state),
            "deadtime_on_confidence": state.deadtime_on_confidence,
            "deadtime_on_locked": state.deadtime_on_locked,
            "deadtime_off_cycles": state.deadtime_off_cycles,
            "deadtime_off_minutes": _published_deadtime_off_minutes(state),
            "deadtime_off_confidence": state.deadtime_off_confidence,
            "deadtime_off_locked": state.deadtime_off_locked,
            "deadtime_pending_step": state.deadtime_pending_step,
            "deadtime_best_candidate": state.deadtime_best_candidate,
            "deadtime_second_best_candidate": state.deadtime_second_best_candidate,
            "calculated_on_percent": state.calculated_on_percent,
            "requested_on_percent": state.requested_on_percent,
            "committed_on_percent": state.committed_on_percent,
            "actuator_mode": state.actuator_mode,
            "valve_curve_params": state.valve_curve_params,
            "valve_curve_learning_enabled": state.valve_curve_learning_enabled,
            "valve_curve_converged": state.valve_curve_converged,
            "valve_curve_observations_accepted": state.valve_curve_observations_accepted,
            "valve_curve_observations_rejected": state.valve_curve_observations_rejected,
            "valve_curve_rejected_updates": state.valve_curve_rejected_updates,
            "valve_curve_last_reason": state.valve_curve_last_reason,
        }
    return data
