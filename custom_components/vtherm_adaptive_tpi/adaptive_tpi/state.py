"""State objects for Adaptive TPI."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class AdaptiveTPIState:
    """Compact mutable state for the algorithm scaffold."""

    k_int: float
    k_ext: float
    nd_hat: float = 0.0
    a_hat: float = 0.0
    b_hat: float = 0.0
    on_percent: float = 0.0
    calculated_on_percent: float = 0.0
    bootstrap_phase: str = "startup"
    last_freeze_reason: str | None = None

