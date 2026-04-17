"""Supervisor scaffolding for Adaptive TPI."""

from __future__ import annotations

from dataclasses import dataclass
from collections import deque

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
        self._a_history: deque[float] = deque(maxlen=11)
        self._b_history: deque[float] = deque(maxlen=11)

    def reset(self) -> None:
        """Reset the supervisor state."""
        self.phase = PHASE_STARTUP
        self.last_freeze_reason = None
        self.last_decision = SupervisorDecision()
        self._a_history.clear()
        self._b_history.clear()

    def set_phase(self, phase: str) -> None:
        """Force the current bootstrap phase."""
        self.phase = self._normalize_phase(phase)

    def advance_phase(self, next_phase: str | None = None) -> str:
        """Advance to the next bootstrap phase or force one explicitly."""
        if next_phase is not None:
            self.phase = self._normalize_phase(next_phase)
            if self.phase == PHASE_C:
                self._a_history.clear()
                self._b_history.clear()
            return self.phase

        current_index = SUPERVISOR_PHASES.index(self.phase)
        if current_index < len(SUPERVISOR_PHASES) - 1:
            self.phase = SUPERVISOR_PHASES[current_index + 1]
        if self.phase == PHASE_C:
            self._a_history.clear()
            self._b_history.clear()
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

    def allow_estimator_update(self, *, deadtime_locked: bool, c_nd: float) -> bool:
        """Return True when the estimator may consume the current accepted cycle."""
        if not deadtime_locked:
            self.last_freeze_reason = "deadtime_not_locked"
            return False

        if c_nd < CONFIDENCE_LOCK_THRESHOLD:
            self.last_freeze_reason = "deadtime_confidence_low"
            return False

        self.last_freeze_reason = None
        return True

    def update_phase_progression(
        self,
        state: AdaptiveTPIState,
        *,
        deadtime_costs_available: bool,
        estimator_updated: bool,
    ) -> str:
        """Apply the v1 bootstrap progression rules to the runtime state."""
        if self.phase == PHASE_STARTUP and state.valid_cycles_count > 0:
            self.advance_phase(PHASE_A)

        if self.phase == PHASE_A:
            if (
                state.valid_cycles_count >= 5
                and state.informative_deadtime_cycles_count >= 3
            ):
                self.advance_phase(PHASE_B)

        if self.phase == PHASE_B:
            if state.deadtime_locked and state.c_nd >= CONFIDENCE_LOCK_THRESHOLD:
                self.advance_phase(PHASE_C)
                state.adaptive_cycles_since_phase_c = 0

        if estimator_updated:
            self._a_history.append(state.a_hat)
            self._b_history.append(state.b_hat)
            if self.phase in (PHASE_C, PHASE_D):
                state.adaptive_cycles_since_phase_c += 1

        if self.phase == PHASE_C and self._phase_c_exit_ready(state):
            self.advance_phase(PHASE_D)

        if self.phase == PHASE_A and not deadtime_costs_available and self.last_decision.classification == "accepted":
            self.last_freeze_reason = "deadtime_observation_window_too_short"

        return self.phase

    def finalize_non_informative_cycle(self, reason: str = "non_informative_cycle") -> SupervisorDecision:
        """Record the cycle as valid but non-informative after runtime checks passed."""
        self.last_freeze_reason = reason
        return self.mark_non_informative(reason)

    def apply_to_state(self, state: AdaptiveTPIState) -> None:
        """Synchronize the supervisor placeholders into the runtime state."""
        state.bootstrap_phase = self.phase
        state.last_freeze_reason = self.last_freeze_reason
        state.last_cycle_classification = self.last_decision.classification

    @staticmethod
    def _normalize_phase(phase: str) -> str:
        """Return a known bootstrap phase."""
        if phase in SUPERVISOR_PHASES:
            return phase
        return PHASE_STARTUP

    def _phase_c_exit_ready(self, state: AdaptiveTPIState) -> bool:
        """Return True when the initial convergence phase may exit."""
        if state.c_a < 0.6 or state.c_b < 0.5:
            return False

        if state.adaptive_cycles_since_phase_c < 20:
            return False

        if len(self._a_history) < 11 or len(self._b_history) < 11:
            return False

        a_motion = abs(self._a_history[-1] - self._a_history[0]) / max(state.a_hat, 1e-3)
        b_motion = abs(self._b_history[-1] - self._b_history[0]) / max(state.b_hat, 1e-3)
        return a_motion < 0.10 and b_motion < 0.10
