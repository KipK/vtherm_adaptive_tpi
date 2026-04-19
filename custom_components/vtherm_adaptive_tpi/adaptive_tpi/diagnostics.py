"""Diagnostics helpers for Adaptive TPI."""

from __future__ import annotations

from .state import AdaptiveTPIState


def build_diagnostics(state: AdaptiveTPIState, debug_mode: bool) -> dict:
    """Build the stable diagnostics payload exposed by the algorithm."""
    data = {
        "bootstrap_phase": state.bootstrap_phase,
        "phase": state.bootstrap_phase,
        "k_int": state.k_int,
        "k_ext": state.k_ext,
        "nd_hat": state.nd_hat,
        "c_nd": state.c_nd,
        "a_hat": state.a_hat,
        "b_hat": state.b_hat,
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
        "a_dispersion": state.a_dispersion,
        "b_dispersion": state.b_dispersion,
        "deadtime_candidate_costs": state.deadtime_candidate_costs,
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
            "deadtime_best_candidate": state.deadtime_best_candidate,
            "deadtime_second_best_candidate": state.deadtime_second_best_candidate,
            "calculated_on_percent": state.calculated_on_percent,
            "on_percent": state.on_percent,
        }
    return data
