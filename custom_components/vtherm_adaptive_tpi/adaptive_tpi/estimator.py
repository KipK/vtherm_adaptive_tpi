"""Parameter estimation scaffolding for Adaptive TPI."""

from __future__ import annotations


class ParameterEstimator:
    """Placeholder estimator for `a_hat` and `b_hat`."""

    def __init__(self) -> None:
        """Initialize the placeholder estimator."""
        self.a_hat = 0.0
        self.b_hat = 0.0
        self.c_a = 0.0
        self.c_b = 0.0

    def reset(self) -> None:
        """Reset estimator state."""
        self.a_hat = 0.0
        self.b_hat = 0.0
        self.c_a = 0.0
        self.c_b = 0.0

