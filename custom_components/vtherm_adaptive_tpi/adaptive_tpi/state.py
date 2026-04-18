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

    def to_persisted_dict(self) -> dict[str, float | str]:
        """Return the minimal state that must survive restarts."""
        return {
            "k_int": self.k_int,
            "k_ext": self.k_ext,
            "nd_hat": self.nd_hat,
            "a_hat": self.a_hat,
            "b_hat": self.b_hat,
            "bootstrap_phase": self.bootstrap_phase,
        }

    def apply_persisted_dict(self, data: Mapping[str, Any]) -> None:
        """Restore persisted values while keeping deterministic fallbacks."""
        float_fields = ("k_int", "k_ext", "nd_hat", "a_hat", "b_hat")
        for field_name in float_fields:
            value = _coerce_float(data.get(field_name))
            if value is not None:
                setattr(self, field_name, value)

        bootstrap_phase = _coerce_bootstrap_phase(data.get("bootstrap_phase"))
        if bootstrap_phase is not None:
            self.bootstrap_phase = bootstrap_phase

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
