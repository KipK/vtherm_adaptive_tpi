"""Supervisor scaffolding for Adaptive TPI."""

from __future__ import annotations

from dataclasses import dataclass

from .deadtime import CONFIDENCE_LOCK_THRESHOLD
from .state import AdaptiveTPIState, DEFAULT_BOOTSTRAP_PHASE

PHASE_STARTUP = DEFAULT_BOOTSTRAP_PHASE
PHASE_A = "phase_a"
PHASE_B = "phase_b"
PHASE_C = "phase_c"
PHASE_D = "phase_d"

SUPERVISOR_PHASES = (
    PHASE_STARTUP,
    PHASE_A,
    PHASE_B,
    PHASE_C,
    PHASE_D,
)


@dataclass(slots=True)
class SupervisorDecision:
    """Represent the current cycle qualification outcome."""

    classification: str = "idle"
    reason: str | None = None


class AdaptiveTPISupervisor:
    """Bootstrap and freeze skeleton for the Adaptive TPI runtime."""

    def __init__(self, phase: str = PHASE_STARTUP) -> None:
        """Initialize the supervisor with a known bootstrap phase."""
        self.phase = self._normalize_phase(phase)
        self.last_freeze_reason: str | None = None
        self.last_decision = SupervisorDecision()

    def reset(self) -> None:
        """Reset the supervisor state."""
        self.phase = PHASE_STARTUP
        self.last_freeze_reason = None
        self.last_decision = SupervisorDecision()

    def set_phase(self, phase: str) -> None:
        """Force the current bootstrap phase."""
        self.phase = self._normalize_phase(phase)

    def advance_phase(self) -> str:
        """Advance to the next bootstrap phase placeholder."""
        current_index = SUPERVISOR_PHASES.index(self.phase)
        if current_index < len(SUPERVISOR_PHASES) - 1:
            self.phase = SUPERVISOR_PHASES[current_index + 1]
        return self.phase

    def reject_cycle(self, reason: str) -> SupervisorDecision:
        """Record a hard cycle rejection and freeze reason."""
        self.last_freeze_reason = reason
        self.last_decision = SupervisorDecision(
            classification="rejected",
            reason=reason,
        )
        return self.last_decision

    def mark_non_informative(self, reason: str = "non_informative_cycle") -> SupervisorDecision:
        """Record a valid but non-informative cycle."""
        self.last_decision = SupervisorDecision(
            classification="non_informative",
            reason=reason,
        )
        return self.last_decision

    def accept_cycle(self) -> SupervisorDecision:
        """Record an accepted cycle without estimator updates yet."""
        self.last_freeze_reason = None
        self.last_decision = SupervisorDecision(classification="accepted")
        return self.last_decision

    def evaluate_runtime_conditions(
        self,
        *,
        target_temp: float | None,
        current_temp: float | None,
        outdoor_temp: float | None,
        hvac_mode,
        power_shedding: bool = False,
        heating_failure: bool = False,
        cycle_interrupted: bool = False,
        central_boiler_unavailable: bool = False,
        startup_blackout_active: bool = False,
        setpoint_transition_active: bool = False,
    ) -> SupervisorDecision:
        """Classify the current runtime conditions using the skeleton rules."""
        if target_temp is None or current_temp is None or outdoor_temp is None:
            return self.reject_cycle("missing_temperature")

        if hvac_mode is None or str(hvac_mode).lower().endswith("off"):
            return self.reject_cycle("hvac_mode_incompatible")

        if cycle_interrupted:
            return self.reject_cycle("cycle_interrupted")

        if power_shedding:
            return self.reject_cycle("power_shedding")

        if heating_failure:
            return self.reject_cycle("heating_failure")

        if central_boiler_unavailable:
            return self.reject_cycle("central_boiler_unavailable")

        if startup_blackout_active:
            return self.reject_cycle("startup_blackout")

        if setpoint_transition_active:
            return self.reject_cycle("setpoint_transition")

        return self.accept_cycle()

    def apply_deadtime_result(self, *, locked: bool, confidence: float, lock_reason: str | None) -> None:
        """Freeze adaptation while deadtime remains uncertain."""
        if not locked:
            self.last_freeze_reason = lock_reason or "deadtime_not_locked"
            return

        if confidence < CONFIDENCE_LOCK_THRESHOLD:
            self.last_freeze_reason = "deadtime_confidence_low"
            return

        self.last_freeze_reason = None

    def apply_to_state(self, state: AdaptiveTPIState) -> None:
        """Synchronize the supervisor placeholders into the runtime state."""
        state.bootstrap_phase = self.phase
        state.last_freeze_reason = self.last_freeze_reason

    @staticmethod
    def _normalize_phase(phase: str) -> str:
        """Return a known bootstrap phase."""
        if phase in SUPERVISOR_PHASES:
            return phase
        return PHASE_STARTUP
