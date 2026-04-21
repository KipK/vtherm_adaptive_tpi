"""Diagnostics helpers for Adaptive TPI."""

from __future__ import annotations

from .state import AdaptiveTPIState


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


def build_diagnostics(state: AdaptiveTPIState, debug_mode: bool) -> dict:
    """Build the stable diagnostics payload exposed by the algorithm."""
    cycle_min = state.cycle_min_at_last_accepted_cycle
    a_per_hour = _per_hour(state.a_hat, cycle_min)
    b_per_hour = _per_hour(state.b_hat, cycle_min)
    tau_h = _tau_h(b_per_hour)
    data = {
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
    }
    if debug_mode:
        data["debug"] = {
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
