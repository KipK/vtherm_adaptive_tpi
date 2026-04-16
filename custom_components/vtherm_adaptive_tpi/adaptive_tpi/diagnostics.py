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
        "last_freeze_reason": state.last_freeze_reason,
    }
    if debug_mode:
        data["calculated_on_percent"] = state.calculated_on_percent
        data["on_percent"] = state.on_percent
    return data
