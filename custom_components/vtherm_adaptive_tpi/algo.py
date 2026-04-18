"""Runtime algorithm wrapper for Adaptive TPI."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Mapping

from .adaptive_tpi.controller import compute_on_percent, project_gains
from .adaptive_tpi.deadtime import DeadtimeModel, DeadtimeObservation
from .adaptive_tpi.diagnostics import build_diagnostics
from .adaptive_tpi.estimator import ParameterEstimator, build_estimator_sample
from .adaptive_tpi.state import AdaptiveTPIState
from .adaptive_tpi.supervisor import AdaptiveTPISupervisor, PHASE_A, PHASE_B
from .const import DEFAULT_KEXT, DEFAULT_KINT

_LOGGER = logging.getLogger(__name__)
_CONFIDENCE_DECAY_30_DAYS = 30
_CONFIDENCE_DECAY_90_DAYS = 90
_MIN_DEADTIME_SETPOINT_ERROR = 0.2
_MIN_DEADTIME_OUTDOOR_DELTA = 1.0
_MIN_DEADTIME_APPLIED_POWER = 0.15
_MAX_DEADTIME_APPLIED_POWER = 0.85


@dataclass(slots=True)
class CycleSample:
    """One committed master cycle captured at the scheduler boundary."""

    target_temp: float
    current_temp: float
    outdoor_temp: float
    applied_power: float
    hvac_mode: Any


class AdaptiveTPIAlgorithm:
    """Minimal runtime-safe Adaptive TPI algorithm scaffold."""

    def __init__(
        self,
        name: str,
        max_on_percent: float | None = None,
        debug_mode: bool = False,
    ) -> None:
        """Initialize the algorithm scaffold."""
        self._name = name
        self._debug_mode = debug_mode
        self._max_on_percent = max_on_percent
        self._state = AdaptiveTPIState(
            k_int=DEFAULT_KINT,
            k_ext=DEFAULT_KEXT,
        )
        self._supervisor = AdaptiveTPISupervisor(phase=self._state.bootstrap_phase)
        self._deadtime_model = DeadtimeModel()
        self._estimator = ParameterEstimator()
        self._state.a_hat = self._estimator.a_hat
        self._state.b_hat = self._estimator.b_hat
        self._last_accepted_at: datetime | None = None
        self._temperature_available = False
        self._pending_cycle_sample: CycleSample | None = None

    def calculate(
        self,
        target_temp: float | None,
        current_temp: float | None,
        ext_current_temp: float | None,
        slope: float | None,
        hvac_mode,
        **kwargs,
    ) -> None:
        """Compute the current on_percent using the placeholder controller."""
        del slope

        decision = self._supervisor.evaluate_runtime_conditions(
            target_temp=target_temp,
            current_temp=current_temp,
            outdoor_temp=ext_current_temp,
            hvac_mode=hvac_mode,
            power_shedding=bool(kwargs.get("power_shedding", False)),
            heating_failure=bool(kwargs.get("heating_failure", False)),
            cycle_interrupted=bool(kwargs.get("cycle_interrupted", False)),
            central_boiler_unavailable=bool(kwargs.get("central_boiler_unavailable", False)),
            startup_blackout_active=bool(kwargs.get("startup_blackout_active", False)),
            setpoint_transition_active=bool(kwargs.get("setpoint_transition_active", False)),
        )
        self._supervisor.apply_to_state(self._state)

        if target_temp is None or current_temp is None:
            self._state.calculated_on_percent = 0.0
            self._temperature_available = False
            return

        self._temperature_available = True
        self._state.calculated_on_percent = compute_on_percent(
            hvac_mode=hvac_mode,
            target_temp=target_temp,
            current_temp=current_temp,
            outdoor_temp=ext_current_temp,
            k_int=self._state.k_int,
            k_ext=self._state.k_ext,
            max_on_percent=self._max_on_percent,
        )
        self._state.on_percent = self._state.calculated_on_percent

    def update_realized_power(self, power_percent: float) -> None:
        """Record the power effectively applied after scheduler constraints."""
        self._state.on_percent = max(0.0, min(1.0, power_percent))

    def on_cycle_started(
        self,
        *,
        on_time_sec: float,
        off_time_sec: float,
        on_percent: float,
        hvac_mode,
        target_temp: float | None,
        current_temp: float | None,
        ext_current_temp: float | None,
    ) -> None:
        """Capture the committed cycle start conditions from the scheduler."""
        del on_time_sec, off_time_sec
        self.update_realized_power(on_percent)
        if target_temp is None or current_temp is None or ext_current_temp is None:
            self._pending_cycle_sample = None
            return

        self._pending_cycle_sample = CycleSample(
            target_temp=target_temp,
            current_temp=current_temp,
            outdoor_temp=ext_current_temp,
            applied_power=self._state.on_percent,
            hvac_mode=hvac_mode,
        )

    def on_cycle_completed(
        self,
        *,
        e_eff: float | None = None,
        elapsed_ratio: float = 1.0,
        cycle_duration_min: float | None = None,
        target_temp: float | None,
        current_temp: float | None,
        ext_current_temp: float | None,
        hvac_mode,
        power_shedding: bool = False,
        **_kwargs,
    ) -> None:
        """Consume one completed master cycle for adaptive learning."""
        pending_cycle = self._pending_cycle_sample
        self._pending_cycle_sample = None

        if e_eff is not None:
            self.update_realized_power(e_eff)

        if pending_cycle is None:
            self.reject_cycle("missing_cycle_context")
            return

        if elapsed_ratio < 1.0:
            self.reject_cycle("cycle_interrupted")
            return

        decision = self._supervisor.evaluate_runtime_conditions(
            target_temp=target_temp,
            current_temp=current_temp,
            outdoor_temp=ext_current_temp,
            hvac_mode=hvac_mode,
            power_shedding=power_shedding,
        )
        self._supervisor.apply_to_state(self._state)
        if decision.classification != "accepted":
            return

        self._last_accepted_at = self._utc_now()
        self._state.valid_cycles_count += 1
        self._state.accepted_cycles_count += 1
        if isinstance(cycle_duration_min, (int, float)):
            self._state.cycle_min_at_last_accepted_cycle = float(cycle_duration_min)

        if not self._is_deadtime_informative_cycle(pending_cycle):
            self._increment_hours_without_excitation(cycle_duration_min)
            self._supervisor.finalize_non_informative_cycle()
            self._supervisor.update_phase_progression(
                self._state,
                deadtime_costs_available=False,
                estimator_updated=False,
            )
            self._supervisor.apply_to_state(self._state)
            return

        self._state.hours_without_excitation = 0.0
        deadtime_result = self._deadtime_model.record_accepted_observation(
            DeadtimeObservation(
                tin=pending_cycle.current_temp,
                tout=pending_cycle.outdoor_temp,
                target_temp=pending_cycle.target_temp,
                applied_power=self._state.on_percent,
            )
        )
        self._state.nd_hat = deadtime_result.nd_hat
        self._state.c_nd = deadtime_result.c_nd
        self._state.deadtime_locked = deadtime_result.locked
        self._state.deadtime_best_candidate = deadtime_result.best_candidate
        self._state.deadtime_second_best_candidate = deadtime_result.second_best_candidate
        self._state.deadtime_candidate_costs = deadtime_result.candidate_costs
        if deadtime_result.candidate_costs:
            self._state.informative_deadtime_cycles_count += 1
        self._supervisor.apply_deadtime_result(
            locked=deadtime_result.locked,
            confidence=deadtime_result.c_nd,
            lock_reason=deadtime_result.lock_reason,
        )
        estimator_sample = build_estimator_sample(
            self._deadtime_model.accepted_observations,
            nd_hat=self._state.nd_hat,
            c_nd=self._state.c_nd,
        )
        estimator_updated = False
        if estimator_sample is not None:
            self._state.i_a = estimator_sample.i_a
            self._state.i_b = estimator_sample.i_b
        if estimator_sample is not None and estimator_sample.i_global <= 0.0:
            self._supervisor.finalize_non_informative_cycle()
        elif self._supervisor.allow_estimator_update(
            deadtime_locked=deadtime_result.locked,
            c_nd=deadtime_result.c_nd,
        ):
            estimator_update = self._estimator.update(estimator_sample)
            self._state.a_hat = estimator_update.a_hat
            self._state.b_hat = estimator_update.b_hat
            self._state.c_a = estimator_update.c_a
            self._state.c_b = estimator_update.c_b
            self._state.i_a = estimator_update.i_a
            self._state.i_b = estimator_update.i_b
            estimator_updated = estimator_update.updated
        self._supervisor.update_phase_progression(
            self._state,
            deadtime_costs_available=bool(deadtime_result.candidate_costs),
            estimator_updated=estimator_updated,
        )
        self._refresh_projected_gains()
        self._supervisor.apply_to_state(self._state)

    @property
    def has_pending_learning_update(self) -> bool:
        """Return True when the runtime state has meaningful adaptive history."""
        return self._state.valid_cycles_count > 0 or self._last_accepted_at is not None

    def save_state(self) -> dict:
        """Return a persistable algorithm snapshot."""
        return self._state.to_persisted_dict()

    def persistence_metadata(self, *, cycle_min: float) -> dict[str, Any]:
        """Return the persistence metadata required for safe warm starts."""
        return {
            "cycle_min": cycle_min,
            "saved_at": self._utc_now().isoformat(),
            "last_accepted_at": (
                self._last_accepted_at.isoformat() if self._last_accepted_at is not None else None
            ),
        }

    def load_state(
        self,
        data: Mapping[str, Any] | None,
        *,
        current_cycle_min: float | None = None,
        persisted_cycle_min: float | None = None,
        last_accepted_at: str | None = None,
        saved_at: str | None = None,
    ) -> None:
        """Load a persistable algorithm snapshot."""
        if not data:
            return
        try:
            self._state.apply_persisted_dict(data)
            self._last_accepted_at = self._parse_datetime(last_accepted_at)
            self._apply_persistence_invalidation(
                current_cycle_min=current_cycle_min,
                persisted_cycle_min=persisted_cycle_min,
                saved_at=saved_at,
                last_accepted_at=last_accepted_at,
            )
            self._estimator.restore(
                a_hat=self._state.a_hat,
                b_hat=self._state.b_hat,
                c_a=self._state.c_a,
                c_b=self._state.c_b,
            )
            self._state.a_hat = self._estimator.a_hat
            self._state.b_hat = self._estimator.b_hat
            self._state.c_a = self._estimator.c_a
            self._state.c_b = self._estimator.c_b
            self._supervisor.set_phase(self._state.bootstrap_phase)
            self._supervisor.apply_to_state(self._state)
            self._refresh_projected_gains()
        except (AttributeError, TypeError, ValueError) as err:
            _LOGGER.warning(
                "%s - Ignoring invalid persisted Adaptive TPI state: %s",
                self._name,
                err,
            )

    def get_diagnostics(self) -> dict:
        """Return a compact diagnostics payload."""
        self._supervisor.apply_to_state(self._state)
        return build_diagnostics(self._state, self._debug_mode)

    def reject_cycle(self, reason: str) -> None:
        """Record a hard cycle rejection through the supervisor."""
        self._supervisor.reject_cycle(reason)
        self._supervisor.apply_to_state(self._state)

    def mark_non_informative_cycle(self, reason: str = "non_informative_cycle") -> None:
        """Record a valid but non-informative cycle through the supervisor."""
        self._supervisor.mark_non_informative(reason)
        self._supervisor.apply_to_state(self._state)

    def accept_cycle(self) -> None:
        """Record an accepted cycle through the supervisor."""
        self._supervisor.accept_cycle()
        self._supervisor.apply_to_state(self._state)

    @property
    def on_percent(self) -> float | None:
        """Return the currently applied heating fraction."""
        if not self._temperature_available:
            return None
        return self._state.on_percent

    @property
    def calculated_on_percent(self) -> float:
        """Return the raw calculated heating fraction."""
        return self._state.calculated_on_percent

    def _apply_persistence_invalidation(
        self,
        *,
        current_cycle_min: float | None,
        persisted_cycle_min: float | None,
        saved_at: str | None,
        last_accepted_at: str | None,
    ) -> None:
        """Adjust persisted trust depending on runtime context changes."""
        if (
            isinstance(current_cycle_min, (int, float))
            and isinstance(persisted_cycle_min, (int, float))
            and persisted_cycle_min > 0
            and current_cycle_min > 0
            and abs(current_cycle_min - persisted_cycle_min) > 1e-9
        ):
            ratio = current_cycle_min / persisted_cycle_min
            self._state.a_hat = max(1e-3, self._state.a_hat * ratio)
            self._state.b_hat = max(0.0, self._state.b_hat * ratio)
            self._state.reset_confidences()
            self._state.bootstrap_phase = PHASE_A
            self._supervisor.set_phase(PHASE_A)
            self._supervisor.last_freeze_reason = "cycle_min_changed_revalidation"
            self._state.last_freeze_reason = self._supervisor.last_freeze_reason
            return

        reference_time = self._parse_datetime(last_accepted_at) or self._parse_datetime(saved_at)
        if reference_time is None:
            return

        age_days = (self._utc_now() - reference_time).days
        if age_days > _CONFIDENCE_DECAY_90_DAYS:
            self._state.reset_confidences()
            self._state.bootstrap_phase = PHASE_A
            self._supervisor.set_phase(PHASE_A)
            self._supervisor.last_freeze_reason = "warm_start_revalidation_required"
            self._state.last_freeze_reason = self._supervisor.last_freeze_reason
            return

        if age_days > _CONFIDENCE_DECAY_30_DAYS:
            self._state.decay_confidences(0.5)
            if not self._state.deadtime_locked:
                self._state.bootstrap_phase = PHASE_B
                self._supervisor.set_phase(PHASE_B)
            self._supervisor.last_freeze_reason = "warm_start_confidence_decay"
            self._state.last_freeze_reason = self._supervisor.last_freeze_reason

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        """Parse an ISO datetime, tolerating invalid payloads."""
        if not value or not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _utc_now() -> datetime:
        """Return the current timezone-aware UTC time."""
        return datetime.now(timezone.utc)

    @staticmethod
    def _is_deadtime_informative_cycle(sample: CycleSample) -> bool:
        """Return True when one completed cycle is worth feeding to learning."""
        setpoint_error = abs(sample.target_temp - sample.current_temp)
        outdoor_delta = abs(sample.current_temp - sample.outdoor_temp)
        if setpoint_error < _MIN_DEADTIME_SETPOINT_ERROR:
            return False
        if outdoor_delta < _MIN_DEADTIME_OUTDOOR_DELTA:
            return False
        if sample.applied_power < _MIN_DEADTIME_APPLIED_POWER:
            return False
        if sample.applied_power > _MAX_DEADTIME_APPLIED_POWER:
            return False
        return True

    def _increment_hours_without_excitation(self, cycle_duration_min: float | None) -> None:
        """Track how long learning has been starved of informative cycles."""
        if isinstance(cycle_duration_min, (int, float)) and cycle_duration_min > 0:
            self._state.hours_without_excitation += float(cycle_duration_min) / 60.0

    def _refresh_projected_gains(self) -> None:
        """Update controller gains only on learning boundaries, not sensor refreshes."""
        self._state.k_int, self._state.k_ext = project_gains(
            phase=self._state.bootstrap_phase,
            k_int=self._state.k_int,
            k_ext=self._state.k_ext,
            a_hat=self._state.a_hat,
            b_hat=self._state.b_hat,
            nd_hat=self._state.nd_hat,
            c_nd=self._state.c_nd,
            c_a=self._state.c_a,
            c_b=self._state.c_b,
        )
