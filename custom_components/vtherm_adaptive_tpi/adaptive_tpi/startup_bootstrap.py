"""Startup bootstrap state machine for Adaptive TPI."""

from __future__ import annotations

from dataclasses import dataclass

STARTUP_BOOTSTRAP_IDLE = "idle"
STARTUP_BOOTSTRAP_PREHEAT = "preheat_to_target"
STARTUP_BOOTSTRAP_COOLDOWN = "cooldown_below_target"
STARTUP_BOOTSTRAP_REHEAT = "reheat_to_target"
STARTUP_BOOTSTRAP_COMPLETED = "completed"
STARTUP_BOOTSTRAP_ABANDONED = "abandoned"

STARTUP_BOOTSTRAP_ACTIVE_STAGES = (
    STARTUP_BOOTSTRAP_PREHEAT,
    STARTUP_BOOTSTRAP_COOLDOWN,
    STARTUP_BOOTSTRAP_REHEAT,
)
STARTUP_BOOTSTRAP_MAX_ATTEMPTS = 2
STARTUP_BOOTSTRAP_LOWER_DELTA_C = 0.3


@dataclass(slots=True)
class StartupBootstrapSnapshot:
    """Expose the current bootstrap state and optional command override."""

    active: bool
    stage: str
    attempt: int
    max_attempts: int
    target_temp: float | None
    lower_target_temp: float | None
    command_on_percent: float | None
    completion_reason: str | None


class StartupBootstrapController:
    """Drive a clean OFF->ON startup sequence until deadtime can be observed."""

    def __init__(
        self,
        *,
        lower_delta_c: float = STARTUP_BOOTSTRAP_LOWER_DELTA_C,
        max_attempts: int = STARTUP_BOOTSTRAP_MAX_ATTEMPTS,
    ) -> None:
        """Initialize the startup bootstrap controller."""
        self._lower_delta_c = lower_delta_c
        self._max_attempts = max_attempts
        self.reset()

    def reset(self) -> None:
        """Reset the bootstrap state machine."""
        self._stage = STARTUP_BOOTSTRAP_IDLE
        self._attempt = 0
        self._completion_reason: str | None = None

    def evaluate(
        self,
        *,
        target_temp: float | None,
        current_temp: float | None,
        deadtime_identification_count: int,
        heating_enabled: bool,
    ) -> StartupBootstrapSnapshot:
        """Return the current bootstrap decision and optional power override."""
        if target_temp is None or current_temp is None:
            return self._snapshot(
                target_temp=target_temp,
                command_on_percent=None,
            )

        lower_target_temp = target_temp - self._lower_delta_c
        if not heating_enabled:
            return self._snapshot(
                target_temp=target_temp,
                lower_target_temp=lower_target_temp,
                command_on_percent=None,
            )
        if (
            deadtime_identification_count > 0
            and self._stage in STARTUP_BOOTSTRAP_ACTIVE_STAGES
        ):
            self._stage = STARTUP_BOOTSTRAP_COMPLETED
            self._completion_reason = "deadtime_identified"
            return self._snapshot(
                target_temp=target_temp,
                lower_target_temp=lower_target_temp,
                command_on_percent=None,
            )

        if self._stage == STARTUP_BOOTSTRAP_IDLE:
            if deadtime_identification_count > 0:
                return self._snapshot(
                    target_temp=target_temp,
                    lower_target_temp=lower_target_temp,
                    command_on_percent=None,
                )
            if current_temp >= target_temp:
                self._stage = STARTUP_BOOTSTRAP_COOLDOWN
                self._attempt = 1
            else:
                self._stage = STARTUP_BOOTSTRAP_PREHEAT
                self._attempt = 0
            self._completion_reason = None

        if self._stage == STARTUP_BOOTSTRAP_PREHEAT:
            if current_temp >= target_temp:
                self._stage = STARTUP_BOOTSTRAP_COOLDOWN
                self._attempt = max(self._attempt, 1)
                return self._snapshot(
                    target_temp=target_temp,
                    lower_target_temp=lower_target_temp,
                    command_on_percent=0.0,
                )
            return self._snapshot(
                target_temp=target_temp,
                lower_target_temp=lower_target_temp,
                command_on_percent=1.0,
            )

        if self._stage == STARTUP_BOOTSTRAP_COOLDOWN:
            if current_temp <= lower_target_temp:
                self._stage = STARTUP_BOOTSTRAP_REHEAT
                self._attempt = max(self._attempt, 1)
                return self._snapshot(
                    target_temp=target_temp,
                    lower_target_temp=lower_target_temp,
                    command_on_percent=1.0,
                )
            return self._snapshot(
                target_temp=target_temp,
                lower_target_temp=lower_target_temp,
                command_on_percent=0.0,
            )

        if self._stage == STARTUP_BOOTSTRAP_REHEAT:
            if current_temp >= target_temp:
                if deadtime_identification_count > 0:
                    self._stage = STARTUP_BOOTSTRAP_COMPLETED
                    self._completion_reason = "deadtime_identified"
                    return self._snapshot(
                        target_temp=target_temp,
                        lower_target_temp=lower_target_temp,
                        command_on_percent=None,
                    )
                if self._attempt < self._max_attempts:
                    self._attempt += 1
                    self._stage = STARTUP_BOOTSTRAP_COOLDOWN
                    return self._snapshot(
                        target_temp=target_temp,
                        lower_target_temp=lower_target_temp,
                        command_on_percent=0.0,
                    )
                self._stage = STARTUP_BOOTSTRAP_ABANDONED
                self._completion_reason = "deadtime_not_identified_after_retries"
                return self._snapshot(
                    target_temp=target_temp,
                    lower_target_temp=lower_target_temp,
                    command_on_percent=None,
                )
            return self._snapshot(
                target_temp=target_temp,
                lower_target_temp=lower_target_temp,
                command_on_percent=1.0,
            )

        return self._snapshot(
            target_temp=target_temp,
            lower_target_temp=lower_target_temp,
            command_on_percent=None,
        )

    def _snapshot(
        self,
        *,
        target_temp: float | None,
        lower_target_temp: float | None = None,
        command_on_percent: float | None,
    ) -> StartupBootstrapSnapshot:
        """Build a snapshot of the current state machine status."""
        return StartupBootstrapSnapshot(
            active=self._stage in STARTUP_BOOTSTRAP_ACTIVE_STAGES,
            stage=self._stage,
            attempt=self._attempt,
            max_attempts=self._max_attempts,
            target_temp=target_temp,
            lower_target_temp=lower_target_temp,
            command_on_percent=command_on_percent,
            completion_reason=self._completion_reason,
        )
