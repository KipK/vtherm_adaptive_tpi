"""Runtime algorithm wrapper for Adaptive TPI."""

from __future__ import annotations

import logging
from typing import Any, Mapping

from .adaptive_tpi.controller import compute_on_percent, project_gains
from .adaptive_tpi.deadtime import DeadtimeModel, DeadtimeObservation
from .adaptive_tpi.diagnostics import build_diagnostics
from .adaptive_tpi.estimator import ParameterEstimator, build_estimator_sample
from .adaptive_tpi.state import AdaptiveTPIState
from .adaptive_tpi.supervisor import AdaptiveTPISupervisor
from .const import DEFAULT_KEXT, DEFAULT_KINT

_LOGGER = logging.getLogger(__name__)


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
        self._temperature_available = False

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

        if decision.classification == "accepted" and ext_current_temp is not None and target_temp is not None and current_temp is not None:
            self._state.valid_cycles_count += 1
            deadtime_result = self._deadtime_model.record_accepted_observation(
                DeadtimeObservation(
                    tin=current_temp,
                    tout=ext_current_temp,
                    target_temp=target_temp,
                    applied_power=self._state.on_percent,
                )
            )
            self._state.accepted_cycles_count = self._deadtime_model.accepted_cycle_count
            self._state.nd_hat = deadtime_result.nd_hat
            self._state.c_nd = deadtime_result.c_nd
            self._state.deadtime_locked = deadtime_result.locked
            self._state.deadtime_best_candidate = deadtime_result.best_candidate
            self._state.deadtime_second_best_candidate = deadtime_result.second_best_candidate
            self._state.deadtime_candidate_costs = deadtime_result.candidate_costs
            if deadtime_result.candidate_costs:
                self._state.informative_deadtime_cycles_count += 1
            cycle_min = kwargs.get("cycle_min")
            if isinstance(cycle_min, (int, float)):
                self._state.cycle_min_at_last_accepted_cycle = float(cycle_min)
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
            self._supervisor.apply_to_state(self._state)

        if target_temp is None or current_temp is None:
            self._state.calculated_on_percent = 0.0
            self._temperature_available = False
            return

        self._state.k_int, self._state.k_ext = project_gains(
            phase=self._state.bootstrap_phase,
            k_int=self._state.k_int,
            k_ext=self._state.k_ext,
            a_hat=self._state.a_hat,
            b_hat=self._state.b_hat,
            nd_hat=self._state.nd_hat,
        )
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

    def save_state(self) -> dict:
        """Return a persistable algorithm snapshot."""
        return self._state.to_persisted_dict()

    def load_state(self, data: Mapping[str, Any] | None) -> None:
        """Load a persistable algorithm snapshot."""
        if not data:
            return
        try:
            self._state.apply_persisted_dict(data)
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
