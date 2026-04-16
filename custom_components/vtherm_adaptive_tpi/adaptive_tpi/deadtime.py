"""Deadtime scaffolding for Adaptive TPI."""

from __future__ import annotations


class DeadtimeModel:
    """Placeholder deadtime model."""

    def __init__(self) -> None:
        """Initialize the placeholder model."""
        self.nd_hat = 0.0
        self.confidence = 0.0

    def reset(self) -> None:
        """Reset the deadtime state."""
        self.nd_hat = 0.0
        self.confidence = 0.0

