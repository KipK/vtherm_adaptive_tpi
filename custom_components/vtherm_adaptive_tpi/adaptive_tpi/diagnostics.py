"""Diagnostics helpers for Adaptive TPI."""

from __future__ import annotations

from .state import AdaptiveTPIState


def build_diagnostics(state: AdaptiveTPIState, debug_mode: bool) -> dict:
    """Build a compact diagnostics payload."""
    data = {
        "bootstrap_phase": state.bootstrap_phase,
        "k_int": state.k_int,
        "k_ext": state.k_ext,
        "nd_hat": state.nd_hat,
        "a_hat": state.a_hat,
        "b_hat": state.b_hat,
        "c_nd": state.c_nd,
        "c_a": state.c_a,
        "c_b": state.c_b,
        "i_a": state.i_a,
        "i_b": state.i_b,
        "accepted_cycles_count": state.accepted_cycles_count,
        "hours_without_excitation": state.hours_without_excitation,
        "cycle_min_at_last_accepted_cycle": state.cycle_min_at_last_accepted_cycle,
        "deadtime_locked": state.deadtime_locked,
        "deadtime_best_candidate": state.deadtime_best_candidate,
        "deadtime_second_best_candidate": state.deadtime_second_best_candidate,
        "deadtime_candidate_costs": state.deadtime_candidate_costs,
        "last_freeze_reason": state.last_freeze_reason,
    }
    if debug_mode:
        data["calculated_on_percent"] = state.calculated_on_percent
        data["on_percent"] = state.on_percent
    return data
