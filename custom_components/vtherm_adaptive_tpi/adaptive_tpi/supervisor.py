"""Supervisor scaffolding for Adaptive TPI."""

from __future__ import annotations


class AdaptiveTPISupervisor:
    """Placeholder supervisor for bootstrap and freeze logic."""

    def __init__(self) -> None:
        """Initialize the placeholder supervisor."""
        self.phase = "startup"
        self.last_freeze_reason: str | None = None

    def reset(self) -> None:
        """Reset the supervisor state."""
        self.phase = "startup"
        self.last_freeze_reason = None

