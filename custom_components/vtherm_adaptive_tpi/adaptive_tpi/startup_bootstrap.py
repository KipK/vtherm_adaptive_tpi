"""Startup bootstrap state machine for Adaptive TPI."""

from __future__ import annotations

from dataclasses import dataclass

STARTUP_BOOTSTRAP_IDLE = "idle"
STARTUP_BOOTSTRAP_PREHEAT = "preheat_to_target"
STARTUP_BOOTSTRAP_COOLDOWN = "cooldown_below_target"
STARTUP_BOOTSTRAP_REHEAT = "reheat_to_upper_target"
STARTUP_BOOTSTRAP_FINAL_COOLDOWN = "cooldown_to_target"
STARTUP_BOOTSTRAP_COMPLETED = "completed"
STARTUP_BOOTSTRAP_ABANDONED = "abandoned"

STARTUP_BOOTSTRAP_ACTIVE_STAGES = (
    STARTUP_BOOTSTRAP_PREHEAT,
    STARTUP_BOOTSTRAP_COOLDOWN,
    STARTUP_BOOTSTRAP_REHEAT,
    STARTUP_BOOTSTRAP_FINAL_COOLDOWN,
)
STARTUP_BOOTSTRAP_MAX_ATTEMPTS = 0
STARTUP_BOOTSTRAP_LOWER_DELTA_C = 0.5
STARTUP_BOOTSTRAP_UPPER_DELTA_C = 0.3


@dataclass(slots=True)
class StartupBootstrapSnapshot:
    """Expose the current bootstrap state and optional command override."""

    active: bool
    stage: str
    attempt: int
    max_attempts: int
    target_temp: float | None
    lower_target_temp: float | None
    upper_target_temp: float | None
    command_on_percent: float | None
    completion_reason: str | None


class StartupBootstrapController:
    """Drive a clean OFF->ON startup sequence until deadtime can be observed."""

    def __init__(
        self,
        *,
        lower_delta_c: float = STARTUP_BOOTSTRAP_LOWER_DELTA_C,
        upper_delta_c: float = STARTUP_BOOTSTRAP_UPPER_DELTA_C,
        max_attempts: int = STARTUP_BOOTSTRAP_MAX_ATTEMPTS,
    ) -> None:
        """Initialize the startup bootstrap controller."""
        self._lower_delta_c = lower_delta_c
        self._upper_delta_c = upper_delta_c
        self._max_attempts = max_attempts
        self.reset()

    def reset(self) -> None:
        """Reset the bootstrap state machine."""
        self._stage = STARTUP_BOOTSTRAP_IDLE
        self._attempt = 0
        self._completion_reason: str | None = None
        self._force_requested_for_stage = False
        self._force_stage_context = STARTUP_BOOTSTRAP_IDLE

    def evaluate(
        self,
        *,
        target_temp: float | None,
        current_temp: float | None,
        deadtime_identification_count: int,
        deadtime_on_locked: bool = False,
        deadtime_off_locked: bool = False,
        heating_enabled: bool,
        mode_sign: int = 1,
    ) -> StartupBootstrapSnapshot:
        """Return the current bootstrap decision and optional power override."""
        del deadtime_identification_count
        if target_temp is None or current_temp is None:
            return self._snapshot(
                target_temp=target_temp,
                command_on_percent=None,
            )

        # lower_target_temp is the passive-drift threshold used for the OFF step.
        # In HEAT: target - delta (below setpoint).
        # In COOL: target + delta (above setpoint).
        lower_target_temp = target_temp - mode_sign * self._lower_delta_c
        upper_target_temp = target_temp + mode_sign * self._upper_delta_c
        if not heating_enabled:
            return self._snapshot(
                target_temp=target_temp,
                lower_target_temp=lower_target_temp,
                upper_target_temp=upper_target_temp,
                command_on_percent=None,
            )
        both_deadtimes_locked = deadtime_on_locked and deadtime_off_locked
        if (
            both_deadtimes_locked
            and self._stage in STARTUP_BOOTSTRAP_ACTIVE_STAGES
            and self._stage != STARTUP_BOOTSTRAP_FINAL_COOLDOWN
        ):
            self._stage = STARTUP_BOOTSTRAP_FINAL_COOLDOWN
            self._completion_reason = None
            self._sync_force_state()

        if self._stage == STARTUP_BOOTSTRAP_IDLE:
            if both_deadtimes_locked:
                return self._snapshot(
                    target_temp=target_temp,
                    lower_target_temp=lower_target_temp,
                    upper_target_temp=upper_target_temp,
                    command_on_percent=None,
                )
            if mode_sign * (current_temp - lower_target_temp) <= 0:
                self._stage = STARTUP_BOOTSTRAP_REHEAT
            else:
                self._stage = STARTUP_BOOTSTRAP_COOLDOWN
            self._attempt = 1
            self._completion_reason = None
            self._sync_force_state()

        if self._stage == STARTUP_BOOTSTRAP_PREHEAT:
            if mode_sign * (current_temp - target_temp) >= 0:
                self._stage = STARTUP_BOOTSTRAP_COOLDOWN
                self._attempt = max(self._attempt, 1)
                self._sync_force_state()
                return self._snapshot(
                    target_temp=target_temp,
                    lower_target_temp=lower_target_temp,
                    upper_target_temp=upper_target_temp,
                    command_on_percent=0.0,
                )
            return self._snapshot(
                target_temp=target_temp,
                lower_target_temp=lower_target_temp,
                upper_target_temp=upper_target_temp,
                command_on_percent=1.0,
            )

        if self._stage == STARTUP_BOOTSTRAP_COOLDOWN:
            # HEAT: wait until current <= lower_target  →  mode_sign*(current - lower) <= 0
            # COOL: wait until current >= lower_target  →  mode_sign*(current - lower) <= 0
            if mode_sign * (current_temp - lower_target_temp) <= 0:
                self._stage = STARTUP_BOOTSTRAP_REHEAT
                self._attempt = max(self._attempt, 1)
                self._sync_force_state()
                return self._snapshot(
                    target_temp=target_temp,
                    lower_target_temp=lower_target_temp,
                    upper_target_temp=upper_target_temp,
                    command_on_percent=1.0,
                )
            return self._snapshot(
                target_temp=target_temp,
                lower_target_temp=lower_target_temp,
                upper_target_temp=upper_target_temp,
                command_on_percent=0.0,
            )

        if self._stage == STARTUP_BOOTSTRAP_REHEAT:
            if mode_sign * (current_temp - upper_target_temp) >= 0:
                self._stage = STARTUP_BOOTSTRAP_FINAL_COOLDOWN
                self._sync_force_state()
                return self._snapshot(
                    target_temp=target_temp,
                    lower_target_temp=lower_target_temp,
                    upper_target_temp=upper_target_temp,
                    command_on_percent=0.0,
                )
            return self._snapshot(
                target_temp=target_temp,
                lower_target_temp=lower_target_temp,
                upper_target_temp=upper_target_temp,
                command_on_percent=1.0,
            )

        if self._stage == STARTUP_BOOTSTRAP_FINAL_COOLDOWN:
            if mode_sign * (current_temp - target_temp) <= 0:
                if both_deadtimes_locked:
                    self._stage = STARTUP_BOOTSTRAP_COMPLETED
                    self._completion_reason = "deadtime_on_off_identified"
                    self._sync_force_state()
                    return self._snapshot(
                        target_temp=target_temp,
                        lower_target_temp=lower_target_temp,
                        upper_target_temp=upper_target_temp,
                        command_on_percent=None,
                    )
                self._attempt += 1
                self._stage = STARTUP_BOOTSTRAP_COOLDOWN
                self._completion_reason = "deadtime_on_off_retry"
                self._sync_force_state()
            return self._snapshot(
                target_temp=target_temp,
                lower_target_temp=lower_target_temp,
                upper_target_temp=upper_target_temp,
                command_on_percent=0.0,
            )

        return self._snapshot(
            target_temp=target_temp,
            lower_target_temp=lower_target_temp,
            upper_target_temp=upper_target_temp,
            command_on_percent=None,
        )

    def should_force_cycle_restart(
        self,
        *,
        target_temp: float | None,
        current_temp: float | None,
        deadtime_identification_count: int,
        deadtime_on_locked: bool = False,
        deadtime_off_locked: bool = False,
        heating_enabled: bool,
        mode_sign: int = 1,
    ) -> bool:
        """Return True when the current bootstrap stage should end immediately."""
        del deadtime_identification_count
        self._sync_force_state()
        if self._force_requested_for_stage:
            return False
        if target_temp is None or current_temp is None or not heating_enabled:
            return False

        lower_target_temp = target_temp - mode_sign * self._lower_delta_c
        upper_target_temp = target_temp + mode_sign * self._upper_delta_c
        both_deadtimes_locked = deadtime_on_locked and deadtime_off_locked
        if (
            both_deadtimes_locked
            and self._stage in STARTUP_BOOTSTRAP_ACTIVE_STAGES
            and self._stage != STARTUP_BOOTSTRAP_FINAL_COOLDOWN
        ):
            self._stage = STARTUP_BOOTSTRAP_FINAL_COOLDOWN
            self._completion_reason = None
            self._sync_force_state()
            self._force_requested_for_stage = True
            return True
        if self._stage == STARTUP_BOOTSTRAP_PREHEAT:
            if mode_sign * (current_temp - target_temp) >= 0:
                self._force_requested_for_stage = True
                return True
        if self._stage == STARTUP_BOOTSTRAP_REHEAT:
            if mode_sign * (current_temp - upper_target_temp) >= 0:
                self._force_requested_for_stage = True
                return True
        if (
            self._stage == STARTUP_BOOTSTRAP_COOLDOWN
            and mode_sign * (current_temp - lower_target_temp) <= 0
        ):
            self._force_requested_for_stage = True
            return True
        if (
            self._stage == STARTUP_BOOTSTRAP_FINAL_COOLDOWN
            and mode_sign * (current_temp - target_temp) <= 0
        ):
            self._force_requested_for_stage = True
            return True
        return False

    def _snapshot(
        self,
        *,
        target_temp: float | None,
        lower_target_temp: float | None = None,
        upper_target_temp: float | None = None,
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
            upper_target_temp=upper_target_temp,
            command_on_percent=command_on_percent,
            completion_reason=self._completion_reason,
        )

    def _sync_force_state(self) -> None:
        """Reset one-shot force gating when the bootstrap stage changes."""
        if self._force_stage_context == self._stage:
            return
        self._force_stage_context = self._stage
        self._force_requested_for_stage = False
