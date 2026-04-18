"""Diagnostics helpers for Adaptive TPI."""

from __future__ import annotations

from .state import AdaptiveTPIState


def build_diagnostics(state: AdaptiveTPIState, debug_mode: bool) -> dict:
    """Build the stable diagnostics payload exposed by the algorithm."""
    data = {
        "bootstrap_phase": state.bootstrap_phase,
        "phase": state.bootstrap_phase,
        "Kint": state.k_int,
        "Kext": state.k_ext,
        "k_int": state.k_int,
        "k_ext": state.k_ext,
        "nd_hat": state.nd_hat,
        "c_nd": state.c_nd,
        "a_hat": state.a_hat,
        "b_hat": state.b_hat,
        "c_a": state.c_a,
        "c_b": state.c_b,
        "i_a": state.i_a,
        "i_b": state.i_b,
        "deadtime_candidate_costs": state.deadtime_candidate_costs,
        "last_freeze_reason": state.last_freeze_reason,
        "accepted_cycles_count": state.accepted_cycles_count,
        "hours_without_excitation": state.hours_without_excitation,
        "cycle_min_at_last_accepted_cycle": state.cycle_min_at_last_accepted_cycle,
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
