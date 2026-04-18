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
    a_hat: float = 0.0
    b_hat: float = 0.0
    c_nd: float = 0.0
    c_a: float = 0.0
    c_b: float = 0.0
    i_a: float = 0.0
    i_b: float = 0.0
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
    deadtime_locked: bool = False
    deadtime_best_candidate: float | None = None
    deadtime_second_best_candidate: float | None = None
    deadtime_candidate_costs: dict[str, float] = field(default_factory=dict)

    def to_persisted_dict(self) -> dict[str, Any]:
        """Return the adaptive state that must survive restarts."""
        return {
            "k_int": self.k_int,
            "k_ext": self.k_ext,
            "nd_hat": self.nd_hat,
            "a_hat": self.a_hat,
            "b_hat": self.b_hat,
            "c_nd": self.c_nd,
            "c_a": self.c_a,
            "c_b": self.c_b,
            "i_a": self.i_a,
            "i_b": self.i_b,
            "bootstrap_phase": self.bootstrap_phase,
            "valid_cycles_count": self.valid_cycles_count,
            "informative_deadtime_cycles_count": self.informative_deadtime_cycles_count,
            "accepted_cycles_count": self.accepted_cycles_count,
            "adaptive_cycles_since_phase_c": self.adaptive_cycles_since_phase_c,
            "hours_without_excitation": self.hours_without_excitation,
            "cycle_min_at_last_accepted_cycle": self.cycle_min_at_last_accepted_cycle,
            "deadtime_locked": self.deadtime_locked,
            "deadtime_best_candidate": self.deadtime_best_candidate,
            "deadtime_second_best_candidate": self.deadtime_second_best_candidate,
            "deadtime_candidate_costs": dict(self.deadtime_candidate_costs),
            "last_freeze_reason": self.last_freeze_reason,
        }

    def apply_persisted_dict(self, data: Mapping[str, Any]) -> None:
        """Restore persisted values while keeping deterministic fallbacks."""
        float_fields = (
            "k_int",
            "k_ext",
            "nd_hat",
            "a_hat",
            "b_hat",
            "c_nd",
            "c_a",
            "c_b",
            "i_a",
            "i_b",
            "hours_without_excitation",
            "cycle_min_at_last_accepted_cycle",
            "deadtime_best_candidate",
            "deadtime_second_best_candidate",
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
        )
        for field_name in int_fields:
            value = _coerce_int(data.get(field_name))
            if value is not None:
                setattr(self, field_name, value)

        if isinstance(data.get("deadtime_locked"), bool):
            self.deadtime_locked = data["deadtime_locked"]

        candidate_costs = _coerce_str_float_dict(data.get("deadtime_candidate_costs"))
        if candidate_costs is not None:
            self.deadtime_candidate_costs = candidate_costs

        if isinstance(data.get("last_freeze_reason"), str):
            self.last_freeze_reason = data["last_freeze_reason"]

    def reset_confidences(self) -> None:
        """Reset adaptive confidences and transient trust markers."""
        self.c_nd = 0.0
        self.c_a = 0.0
        self.c_b = 0.0
        self.deadtime_locked = False
        self.deadtime_candidate_costs = {}
        self.deadtime_best_candidate = None
        self.deadtime_second_best_candidate = None

    def decay_confidences(self, factor: float) -> None:
        """Decay the stored confidences by a bounded multiplicative factor."""
        bounded_factor = min(max(factor, 0.0), 1.0)
        self.c_nd *= bounded_factor
        self.c_a *= bounded_factor
        self.c_b *= bounded_factor
        if self.c_nd < 0.6:
            self.deadtime_locked = False
