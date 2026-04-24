"""Adaptive window policy for A/B learning window size and signal thresholds."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil


@dataclass(frozen=True, slots=True)
class LearningWindowPolicy:
    """Window size limits and signal thresholds for one learning round."""

    min_amplitude: float
    relaxed_min_amplitude: float
    min_duration_min: float
    max_cycles: int
    max_duration_min: float
    min_directional_steps: int


def build_learning_window_policy(
    *,
    nd_hat: float,
    cycle_duration_min: float,
) -> LearningWindowPolicy:
    """Build a policy scaled to the current cycle duration."""
    del nd_hat
    max_duration_min = 120.0
    max_cycles = max(3, ceil(max_duration_min / max(cycle_duration_min, 1.0)))
    return LearningWindowPolicy(
        min_amplitude=0.08,
        relaxed_min_amplitude=0.05,
        min_duration_min=8.0,
        max_cycles=max_cycles,
        max_duration_min=max_duration_min,
        min_directional_steps=2,
    )
