"""State objects for Adaptive TPI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

PERSISTENCE_SCHEMA_VERSION = 1
DEFAULT_BOOTSTRAP_PHASE = "startup"


def _coerce_float(value: Any) -> float | None:
    """Convert a persisted numeric value to float when possible."""
    if value is None or isinstance(value, bool):
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_bootstrap_phase(value: Any) -> str | None:
    """Convert a persisted bootstrap phase to a usable string."""
    if not isinstance(value, str):
        return None

    phase = value.strip()
    if not phase:
        return None

    return phase


def _coerce_int(value: Any) -> int | None:
    """Convert a persisted integer-like value to int when possible."""
    if value is None or isinstance(value, bool):
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_str_float_dict(value: Any) -> dict[str, float] | None:
    """Convert a persisted mapping of candidate costs to a clean float dict."""
    if not isinstance(value, Mapping):
        return None

    cleaned: dict[str, float] = {}
    for key, raw_value in value.items():
        if not isinstance(key, str):
            continue
        parsed = _coerce_float(raw_value)
        if parsed is None:
            continue
        cleaned[key] = parsed
    return cleaned


@dataclass(slots=True)
class AdaptiveTPIState:
    """Compact mutable state for the algorithm scaffold."""

    k_int: float
    k_ext: float
    nd_hat: float = 0.0
    deadtime_minutes: float | None = None
    a_hat: float = 0.0
    b_hat: float = 0.0
    c_nd: float = 0.0
    c_a: float = 0.0
    c_b: float = 0.0
    b_converged: bool = False
    i_a: float = 0.0
    i_b: float = 0.0
    a_samples_count: int = 0
    b_samples_count: int = 0
    a_last_reason: str | None = None
    b_last_reason: str | None = None
    last_learning_attempt_reason: str | None = None
    last_learning_attempt_regime: str | None = None
    a_dispersion: float = 0.0
    b_dispersion: float = 0.0
    on_percent: float = 0.0
    calculated_on_percent: float = 0.0
    bootstrap_phase: str = DEFAULT_BOOTSTRAP_PHASE
    valid_cycles_count: int = 0
    informative_deadtime_cycles_count: int = 0
    accepted_cycles_count: int = 0
    adaptive_cycles_since_phase_c: int = 0
    last_cycle_classification: str = "idle"
    last_freeze_reason: str | None = None
    hours_without_excitation: float = 0.0
    cycle_min_at_last_accepted_cycle: float | None = None
    cycle_started_calls_count: int = 0
    cycle_completed_calls_count: int = 0
    last_cycle_started_at: str | None = None
    last_cycle_completed_at: str | None = None
    deadtime_locked: bool = False
    deadtime_best_candidate: float | None = None
    deadtime_second_best_candidate: float | None = None
    deadtime_b_proxy: float | None = None
    b_crosscheck_error: float | None = None
    b_methods_consistent: bool = False
    deadtime_identification_count: int = 0
    deadtime_identification_qualities: dict[str, float] = field(default_factory=dict)
    deadtime_pending_step: bool = False
    # These routing diagnostics describe the latest runtime decision only.
    # They are intentionally kept out of persistence so restarts do not restore
    # stale branch-selection information as if it were still current.
    a_learning_enabled: bool = False
    current_cycle_regime: str | None = None
    learning_route_selected: str | None = None
    learning_route_block_reason: str | None = None
    deadtime_learning_blackout_active: bool = False
    startup_bootstrap_active: bool = False
    startup_bootstrap_stage: str = "idle"
    startup_bootstrap_attempt: int = 0
    startup_bootstrap_max_attempts: int = 2
    startup_bootstrap_target_temp: float | None = None
    startup_bootstrap_lower_target_temp: float | None = None
    startup_bootstrap_command_on_percent: float | None = None
    startup_bootstrap_completion_reason: str | None = None

    def to_persisted_dict(self) -> dict[str, Any]:
        """Return the adaptive state that must survive restarts."""
        return {
            "k_int": self.k_int,
            "k_ext": self.k_ext,
            "nd_hat": self.nd_hat,
            "deadtime_minutes": self.deadtime_minutes,
            "a_hat": self.a_hat,
            "b_hat": self.b_hat,
            "c_nd": self.c_nd,
            "c_a": self.c_a,
            "c_b": self.c_b,
            "b_converged": self.b_converged,
            "i_a": self.i_a,
            "i_b": self.i_b,
            "a_samples_count": self.a_samples_count,
            "b_samples_count": self.b_samples_count,
            "a_last_reason": self.a_last_reason,
            "b_last_reason": self.b_last_reason,
            "last_learning_attempt_reason": self.last_learning_attempt_reason,
            "last_learning_attempt_regime": self.last_learning_attempt_regime,
            "a_dispersion": self.a_dispersion,
            "b_dispersion": self.b_dispersion,
            "bootstrap_phase": self.bootstrap_phase,
            "valid_cycles_count": self.valid_cycles_count,
            "informative_deadtime_cycles_count": self.informative_deadtime_cycles_count,
            "accepted_cycles_count": self.accepted_cycles_count,
            "adaptive_cycles_since_phase_c": self.adaptive_cycles_since_phase_c,
            "hours_without_excitation": self.hours_without_excitation,
            "cycle_min_at_last_accepted_cycle": self.cycle_min_at_last_accepted_cycle,
            "cycle_started_calls_count": self.cycle_started_calls_count,
            "cycle_completed_calls_count": self.cycle_completed_calls_count,
            "last_cycle_started_at": self.last_cycle_started_at,
            "last_cycle_completed_at": self.last_cycle_completed_at,
            "deadtime_locked": self.deadtime_locked,
            "deadtime_best_candidate": self.deadtime_best_candidate,
            "deadtime_second_best_candidate": self.deadtime_second_best_candidate,
            "deadtime_b_proxy": self.deadtime_b_proxy,
            "b_crosscheck_error": self.b_crosscheck_error,
            "b_methods_consistent": self.b_methods_consistent,
            "deadtime_identification_qualities": dict(self.deadtime_identification_qualities),
            "last_freeze_reason": self.last_freeze_reason,
        }

    def apply_persisted_dict(self, data: Mapping[str, Any]) -> None:
        """Restore persisted values while keeping deterministic fallbacks."""
        float_fields = (
            "k_int",
            "k_ext",
            "nd_hat",
            "deadtime_minutes",
            "a_hat",
            "b_hat",
            "c_nd",
            "c_a",
            "c_b",
            "a_dispersion",
            "b_dispersion",
            "i_a",
            "i_b",
            "hours_without_excitation",
            "cycle_min_at_last_accepted_cycle",
            "deadtime_best_candidate",
            "deadtime_second_best_candidate",
            "deadtime_b_proxy",
            "b_crosscheck_error",
        )
        for field_name in float_fields:
            value = _coerce_float(data.get(field_name))
            if value is not None:
                setattr(self, field_name, value)

        bootstrap_phase = _coerce_bootstrap_phase(data.get("bootstrap_phase"))
        if bootstrap_phase is not None:
            self.bootstrap_phase = bootstrap_phase

        int_fields = (
            "valid_cycles_count",
            "informative_deadtime_cycles_count",
            "accepted_cycles_count",
            "adaptive_cycles_since_phase_c",
            "a_samples_count",
            "b_samples_count",
            "cycle_started_calls_count",
            "cycle_completed_calls_count",
        )
        for field_name in int_fields:
            value = _coerce_int(data.get(field_name))
            if value is not None:
                setattr(self, field_name, value)

        if isinstance(data.get("deadtime_locked"), bool):
            self.deadtime_locked = data["deadtime_locked"]

        if isinstance(data.get("b_converged"), bool):
            self.b_converged = data["b_converged"]
        if isinstance(data.get("b_methods_consistent"), bool):
            self.b_methods_consistent = data["b_methods_consistent"]

        identification_qualities = _coerce_str_float_dict(
            data.get("deadtime_identification_qualities")
        )
        if identification_qualities is not None:
            self.deadtime_identification_qualities = identification_qualities

        if isinstance(data.get("last_freeze_reason"), str):
            self.last_freeze_reason = data["last_freeze_reason"]
        if isinstance(data.get("a_last_reason"), str):
            self.a_last_reason = data["a_last_reason"]
        if isinstance(data.get("b_last_reason"), str):
            self.b_last_reason = data["b_last_reason"]
        if isinstance(data.get("last_learning_attempt_reason"), str):
            self.last_learning_attempt_reason = data["last_learning_attempt_reason"]
        if isinstance(data.get("last_learning_attempt_regime"), str):
            self.last_learning_attempt_regime = data["last_learning_attempt_regime"]
        if isinstance(data.get("last_cycle_started_at"), str):
            self.last_cycle_started_at = data["last_cycle_started_at"]
        if isinstance(data.get("last_cycle_completed_at"), str):
            self.last_cycle_completed_at = data["last_cycle_completed_at"]

    def reset_confidences(self) -> None:
        """Reset adaptive confidences and transient trust markers."""
        self.c_nd = 0.0
        self.c_a = 0.0
        self.c_b = 0.0
        self.b_converged = False
        self.deadtime_locked = False
        self.deadtime_identification_count = 0
        self.deadtime_identification_qualities = {}
        self.deadtime_pending_step = False
        self.deadtime_best_candidate = None
        self.deadtime_second_best_candidate = None
        self.deadtime_b_proxy = None
        self.b_crosscheck_error = None
        self.b_methods_consistent = False

    def decay_confidences(self, factor: float) -> None:
        """Decay the stored confidences by a bounded multiplicative factor."""
        bounded_factor = min(max(factor, 0.0), 1.0)
        self.c_nd *= bounded_factor
        self.c_a *= bounded_factor
        self.c_b *= bounded_factor
        if self.c_nd < 0.6:
            self.deadtime_locked = False
