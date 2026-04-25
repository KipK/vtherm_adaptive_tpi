"""Runtime algorithm wrapper for Adaptive TPI."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from dataclasses import asdict
from dataclasses import dataclass, replace
from typing import Any, Mapping

from .adaptive_tpi.controller import compute_on_percent, project_gains
from .adaptive_tpi.deadtime import CycleHistoryEntry, DeadtimeModel, DeadtimeObservation
from .adaptive_tpi.deadtime_tracker import DeadtimeTracker
from .adaptive_tpi.diagnostics import build_diagnostics
from .adaptive_tpi.estimator import ASample, BSample, ParameterEstimator
from .adaptive_tpi.learning_window import (
    WINDOW_REGIME_MIXED,
    WINDOW_REGIME_OFF,
    WINDOW_REGIME_ON,
    build_anchored_learning_window,
    build_learning_window,
    classify_cycle_regime,
)
from .adaptive_tpi.startup_bootstrap import (
    STARTUP_BOOTSTRAP_COOLDOWN,
    StartupBootstrapController,
    StartupBootstrapSnapshot,
)
from .adaptive_tpi.mode import hvac_mode_sign
from .adaptive_tpi.state import AdaptiveTPIState
from .adaptive_tpi.supervisor import AdaptiveTPISupervisor, PHASE_B
from .adaptive_tpi.valve_curve import ValveCurveParams, build_valve_curve
from .const import (
    ACTUATOR_MODE_VALVE,
    ACTUATOR_MODE_SWITCH,
    DEFAULT_KEXT,
    DEFAULT_KINT,
    DEFAULT_RESPONSIVENESS,
    RESPONSIVENESS_TO_TAU_CL_MIN,
)

_LOGGER = logging.getLogger(__name__)
_CONFIDENCE_DECAY_30_DAYS = 30
_MIN_DEADTIME_OUTDOOR_DELTA = 1.0
_MIN_ESTIMATOR_SETPOINT_ERROR = 0.2
_MIN_ESTIMATOR_OUTDOOR_DELTA = 1.0
_DEADTIME_B_PROXY_SEED_CONFIDENCE = 0.2
_B_METHODS_CONSISTENT_THRESHOLD = 0.35


@dataclass(slots=True)
class CycleSample:
    """One committed master cycle captured at the scheduler boundary."""

    target_temp: float
    current_temp: float
    outdoor_temp: float
    applied_power: float
    hvac_mode: Any
    bootstrap_b_learning_allowed: bool = False
    applied_demand: float | None = None

    def __post_init__(self) -> None:
        """Keep linear-actuator callers deterministic."""
        if self.applied_demand is None:
            self.applied_demand = self.applied_power


class AdaptiveTPIAlgorithm:
    """Minimal runtime-safe Adaptive TPI algorithm scaffold."""

    def __init__(
        self,
        name: str,
        max_on_percent: float | None = None,
        debug_mode: bool = False,
        responsiveness: int = DEFAULT_RESPONSIVENESS,
        default_kint: float = DEFAULT_KINT,
        default_kext: float = DEFAULT_KEXT,
        actuator_mode: str = ACTUATOR_MODE_SWITCH,
        valve_curve_params: ValveCurveParams | None = None,
        valve_curve_compensation_enabled: bool = True,
        valve_curve_learning_enabled: bool = True,
    ) -> None:
        """Initialize the algorithm scaffold."""
        self._name = name
        self._debug_mode = debug_mode
        self._max_on_percent = max_on_percent
        idx = max(0, min(len(RESPONSIVENESS_TO_TAU_CL_MIN) - 1, responsiveness - 1))
        self._tau_cl_min: float = RESPONSIVENESS_TO_TAU_CL_MIN[idx]
        self._default_kint = default_kint
        self._default_kext = default_kext
        self._actuator_mode = actuator_mode
        self._configured_valve_curve_params = valve_curve_params
        self._valve_curve_compensation_enabled = valve_curve_compensation_enabled
        self._valve_curve_learning_enabled = valve_curve_learning_enabled
        self._valve_curve = build_valve_curve(
            actuator_mode,
            params=valve_curve_params,
            compensation_enabled=valve_curve_compensation_enabled,
            learning_enabled=valve_curve_learning_enabled,
        )
        self._state = AdaptiveTPIState(
            k_int=default_kint,
            k_ext=default_kext,
        )
        self._refresh_valve_curve_state()
        self._supervisor = AdaptiveTPISupervisor(phase=self._state.bootstrap_phase)
        self._deadtime_model = DeadtimeModel()
        self._deadtime_tracker = DeadtimeTracker(now_provider=self._utc_now)
        self._estimator = ParameterEstimator()
        self._startup_bootstrap = StartupBootstrapController()
        self._state.a_hat = self._estimator.a_hat
        self._state.b_hat = self._estimator.b_hat
        self._last_accepted_at: datetime | None = None
        self._temperature_available = False
        self._pending_cycle_sample: CycleSample | None = None

    def _resolve_completed_cycle_sample(
        self,
        sample: CycleSample,
        e_eff: float | None,
    ) -> CycleSample:
        """Return the cycle sample with the actual applied power when known."""
        if e_eff is None:
            return sample
        applied_power = max(0.0, min(1.0, float(e_eff)))
        if abs(applied_power - sample.applied_power) <= 1e-9:
            return sample
        return replace(
            sample,
            applied_power=applied_power,
            applied_demand=self._valve_curve.invert(applied_power),
        )

    def calculate(
        self,
        target_temp: float | None,
        current_temp: float | None,
        ext_current_temp: float | None,
        slope: float | None,
        hvac_mode,
        **kwargs,
    ) -> None:
        """Compute the requested heating fraction for the next cycle."""
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
        sign = hvac_mode_sign(hvac_mode)
        heating_enabled = sign != 0

        if target_temp is None or current_temp is None:
            self._apply_startup_bootstrap_snapshot(
                self._startup_bootstrap.evaluate(
                    target_temp=target_temp,
                    current_temp=current_temp,
                    deadtime_identification_count=self._state.deadtime_identification_count,
                    deadtime_on_locked=self._state.deadtime_on_locked,
                    deadtime_off_locked=self._state.deadtime_off_locked,
                    heating_enabled=heating_enabled,
                    mode_sign=sign,
                )
            )
            self._state.calculated_on_percent = 0.0
            self._state.requested_on_percent = None
            self._state.committed_on_percent = None
            self._temperature_available = False
            return

        self._temperature_available = True
        if self._state.committed_on_percent is None:
            self._state.committed_on_percent = 0.0
        command_on_percent = compute_on_percent(
            hvac_mode=hvac_mode,
            target_temp=target_temp,
            current_temp=current_temp,
            outdoor_temp=ext_current_temp,
            k_int=self._state.k_int,
            k_ext=self._state.k_ext,
            max_on_percent=self._max_on_percent,
            mode_sign=sign,
        )
        bootstrap_snapshot = self._startup_bootstrap.evaluate(
            target_temp=target_temp,
            current_temp=current_temp,
            deadtime_identification_count=self._state.deadtime_identification_count,
            deadtime_on_locked=self._state.deadtime_on_locked,
            deadtime_off_locked=self._state.deadtime_off_locked,
            heating_enabled=heating_enabled,
            mode_sign=sign,
        )
        self._apply_startup_bootstrap_snapshot(bootstrap_snapshot)
        if bootstrap_snapshot.command_on_percent is not None:
            command_on_percent = bootstrap_snapshot.command_on_percent
        self._state.calculated_on_percent = command_on_percent
        self._state.requested_on_percent = self._valve_curve.apply(command_on_percent)

    def update_realized_power(self, power_percent: float) -> None:
        """Record the power committed for the current scheduler cycle."""
        self._state.committed_on_percent = max(0.0, min(1.0, power_percent))

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
        self._state.cycle_started_calls_count += 1
        self._state.last_cycle_started_at = self._utc_now().isoformat()
        previous_committed_on_percent = (
            self._state.committed_on_percent
            if self._state.committed_on_percent is not None
            else 0.0
        )
        self.update_realized_power(on_percent)
        applied_demand = self._valve_curve.invert(self._state.committed_on_percent)
        if target_temp is None or current_temp is None or ext_current_temp is None:
            self._pending_cycle_sample = None
            return
        self._temperature_available = True
        cycle_duration_min = max(0.0, float(on_time_sec + off_time_sec) / 60.0)
        self._deadtime_tracker.on_cycle_started(
            started_at=self._utc_now(),
            current_temp=current_temp,
            target_temp=target_temp,
            on_percent=on_percent,
            previous_on_percent=previous_committed_on_percent,
            mode_sign=hvac_mode_sign(hvac_mode),
            cycle_duration_min=cycle_duration_min,
            cycle_index=len(self._deadtime_model.cycle_history),
            b_proxy=self._deadtime_model.estimate_b_proxy_for_next_step(),
        )
        self._state.deadtime_pending_step = self._deadtime_tracker.has_pending_step

        self._pending_cycle_sample = CycleSample(
            target_temp=target_temp,
            current_temp=current_temp,
            outdoor_temp=ext_current_temp,
            applied_power=self._state.committed_on_percent,
            hvac_mode=hvac_mode,
            bootstrap_b_learning_allowed=(
                self._state.startup_bootstrap_active
                and self._state.startup_bootstrap_stage == STARTUP_BOOTSTRAP_COOLDOWN
                and classify_cycle_regime(applied_demand) == WINDOW_REGIME_OFF
            ),
            applied_demand=applied_demand,
        )

    def on_cycle_completed(
        self,
        *,
        e_eff: float | None = None,
        elapsed_ratio: float = 1.0,
        cycle_duration_min: float | None = None,
        measure_timestamp: datetime | None = None,
        target_temp: float | None,
        current_temp: float | None,
        ext_current_temp: float | None,
        hvac_mode,
        power_shedding: bool = False,
        **_kwargs,
    ) -> None:
        """Consume one completed master cycle for adaptive learning."""
        self._state.cycle_completed_calls_count += 1
        self._state.last_cycle_completed_at = self._utc_now().isoformat()
        pending_cycle = self._pending_cycle_sample
        self._pending_cycle_sample = None

        self.observe_temperature_update(
            current_temp=current_temp,
            target_temp=target_temp,
            measured_at=measure_timestamp,
            hvac_mode=hvac_mode,
        )

        if pending_cycle is None:
            self._state.last_learning_attempt_reason = "missing_cycle_context"
            self._state.last_learning_attempt_regime = None
            self.reject_cycle("missing_cycle_context")
            return
        completed_cycle = self._resolve_completed_cycle_sample(pending_cycle, e_eff)

        if elapsed_ratio < 1.0:
            self._state.last_learning_attempt_reason = "cycle_interrupted"
            self._state.last_learning_attempt_regime = None
            self._record_cycle_history(
                completed_cycle,
                cycle_duration_min=cycle_duration_min or 5.0,
                is_valid=False,
                is_informative=False,
                is_estimator_informative=False,
            )
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
            self._state.last_learning_attempt_reason = decision.reason
            self._state.last_learning_attempt_regime = None
            self._record_cycle_history(
                completed_cycle,
                cycle_duration_min=cycle_duration_min or 5.0,
                is_valid=False,
                is_informative=False,
                is_estimator_informative=False,
            )
            return

        self._last_accepted_at = self._utc_now()
        self._state.valid_cycles_count += 1
        self._state.accepted_cycles_count += 1
        self._state.current_cycle_regime = classify_cycle_regime(completed_cycle.applied_demand)
        self._state.learning_route_selected = "none"
        self._state.learning_route_block_reason = None
        self._state.deadtime_learning_blackout_active = False
        if isinstance(cycle_duration_min, (int, float)):
            self._state.cycle_min_at_last_accepted_cycle = float(cycle_duration_min)

        deadtime_informative = self._is_deadtime_informative_cycle(completed_cycle)
        estimator_informative = self._is_estimator_informative_cycle(completed_cycle)

        if not deadtime_informative and not estimator_informative:
            self._state.last_learning_attempt_reason = "non_informative_cycle"
            self._state.last_learning_attempt_regime = None
            self._state.learning_route_block_reason = "non_informative_cycle"
            self._record_cycle_history(
                completed_cycle,
                cycle_duration_min=cycle_duration_min or 5.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=False,
            )
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
        if deadtime_informative:
            self._state.informative_deadtime_cycles_count += 1
        deadtime_result = self._deadtime_model.record_cycle(
            self._make_deadtime_observation(completed_cycle),
            cycle_duration_min=cycle_duration_min or 5.0,
            is_valid=True,
            is_informative=deadtime_informative,
            is_estimator_informative=estimator_informative,
            bootstrap_b_learning_allowed=completed_cycle.bootstrap_b_learning_allowed,
            mode_sign=hvac_mode_sign(completed_cycle.hvac_mode),
            track_step_response=False,
        )
        self._apply_deadtime_result(deadtime_result)
        if (
            self._state.b_samples_count == 0
            and deadtime_result.best_candidate_b is not None
            and deadtime_result.c_nd >= _DEADTIME_B_PROXY_SEED_CONFIDENCE
        ):
            self._apply_estimator_update(
                self._estimator.seed_b_from_deadtime_proxy(deadtime_result.best_candidate_b)
            )
        estimator_updated = False
        estimator_update = None
        current_cycle_regime = self._state.current_cycle_regime
        a_hat_for_valve_curve = self._state.a_hat
        on_window = None
        valve_curve_window = None

        observations = self._learning_observations_with_terminal(
            target_temp=target_temp,
            current_temp=current_temp,
            ext_current_temp=ext_current_temp,
            cycle_duration_min=cycle_duration_min or 5.0,
            applied_power=completed_cycle.applied_power,
            applied_demand=completed_cycle.applied_demand,
        )
        anchored_end_index = len(observations) - 2

        pending_mode_sign = hvac_mode_sign(completed_cycle.hvac_mode)
        if current_cycle_regime == WINDOW_REGIME_OFF:
            self._state.learning_route_selected = "b"
            nd_hat_for_off = (
                self._state.deadtime_off_cycles
                if self._state.deadtime_off_locked
                and self._state.deadtime_off_cycles is not None
                else 0.0
            )
            off_window = build_anchored_learning_window(
                observations,
                nd_hat=nd_hat_for_off,
                regime=WINDOW_REGIME_OFF,
                end_index=anchored_end_index,
                mode_sign=pending_mode_sign,
            )
            self._state.deadtime_learning_blackout_active = off_window.deadtime_blackout_active
            self._state.last_learning_attempt_regime = "b"
            self._state.last_learning_attempt_reason = off_window.reason
            if off_window.sample is not None and self._supervisor.allow_b_update():
                estimator_update = self._estimator.update_b(
                    BSample(
                        dTdt=off_window.sample.dTdt,
                        delta_out=off_window.sample.delta_out,
                        setpoint_error=off_window.sample.setpoint_error,
                        u_eff=off_window.sample.u_eff,
                        allow_near_setpoint_b=off_window.sample.allow_near_setpoint_b,
                    ),
                    reason=off_window.reason,
                )
            else:
                self._state.learning_route_block_reason = off_window.reason
                self._state.b_last_reason = off_window.reason
        elif current_cycle_regime == WINDOW_REGIME_ON:
            self._state.learning_route_selected = "a"
            on_window = build_anchored_learning_window(
                observations,
                nd_hat=self._state.nd_hat,
                regime=WINDOW_REGIME_ON,
                end_index=anchored_end_index,
                mode_sign=pending_mode_sign,
            )
            self._state.deadtime_learning_blackout_active = on_window.deadtime_blackout_active
            self._state.last_learning_attempt_regime = "a"
            self._state.last_learning_attempt_reason = on_window.reason
            if on_window.sample is not None and self._supervisor.allow_a_update(
                deadtime_locked=deadtime_result.locked,
                c_nd=deadtime_result.c_nd,
                b_converged=self._state.b_converged,
            ):
                estimator_update = self._estimator.update_a(
                    ASample(
                        dTdt=on_window.sample.dTdt,
                        delta_out=on_window.sample.delta_out,
                        setpoint_error=on_window.sample.setpoint_error,
                        u_eff=on_window.sample.u_eff,
                    ),
                    reason=on_window.reason,
                    mode_sign=pending_mode_sign,
                )
            else:
                self._state.learning_route_block_reason = (
                    self._supervisor.last_freeze_reason or on_window.reason
                )
                self._state.last_learning_attempt_reason = self._state.learning_route_block_reason
                self._state.a_last_reason = self._state.learning_route_block_reason
            valve_curve_window = on_window
        else:
            self._state.last_learning_attempt_regime = None
            self._state.last_learning_attempt_reason = "mixed_cycle_regime"
            self._state.learning_route_block_reason = "mixed_cycle_regime"
            if self._actuator_mode == ACTUATOR_MODE_VALVE:
                valve_curve_window = build_anchored_learning_window(
                    observations,
                    nd_hat=self._state.nd_hat,
                    regime=WINDOW_REGIME_MIXED,
                    end_index=anchored_end_index,
                    mode_sign=pending_mode_sign,
                )

        if estimator_update is not None:
            self._apply_estimator_update(estimator_update)
            if self._state.last_learning_attempt_regime == "b":
                self._state.last_learning_attempt_reason = estimator_update.b_last_reason
            else:
                self._state.last_learning_attempt_reason = estimator_update.a_last_reason
            estimator_updated = estimator_update.updated
            self._state.learning_route_block_reason = None
        else:
            self._supervisor.finalize_non_informative_cycle(self._state.last_learning_attempt_reason)
        self._refresh_b_crosscheck()
        if (
            self._actuator_mode == ACTUATOR_MODE_VALVE
            and current_cycle_regime in (WINDOW_REGIME_ON, WINDOW_REGIME_MIXED)
            and valve_curve_window is not None
            and valve_curve_window.sample is not None
        ):
            self._valve_curve.observe(
                u_valve=completed_cycle.applied_power,
                dTdt=valve_curve_window.sample.dTdt,
                delta_out=valve_curve_window.sample.delta_out,
                a_hat=a_hat_for_valve_curve,
                b_hat=self._state.b_hat,
                b_converged=self._state.b_converged,
                mode_sign=pending_mode_sign,
                timestamp=(
                    measure_timestamp.isoformat()
                    if isinstance(measure_timestamp, datetime)
                    else self._utc_now().isoformat()
                ),
            )
            self._refresh_valve_curve_state()
        self._supervisor.update_phase_progression(
            self._state,
            deadtime_costs_available=bool(deadtime_result.candidate_costs),
            estimator_updated=estimator_updated,
        )
        self._state.a_learning_enabled = self._supervisor.is_a_learning_enabled(
            deadtime_locked=self._state.deadtime_locked,
            c_nd=self._state.c_nd,
            b_converged=self._state.b_converged,
        )
        self._refresh_projected_gains()
        self._supervisor.apply_to_state(self._state)

    @property
    def has_pending_learning_update(self) -> bool:
        """Return True when the runtime state has meaningful adaptive history."""
        return self._state.valid_cycles_count > 0 or self._last_accepted_at is not None

    def save_state(self, *, cycle_min: float | None = None) -> dict:
        """Return a persistable algorithm snapshot."""
        return {
            **self._state.to_persisted_dict(cycle_min=cycle_min),
            "deadtime_model": self._deadtime_model.to_persisted_dict(
                cycle_min=cycle_min,
            ),
            "estimator_model": self._estimator.to_persisted_dict(cycle_min=cycle_min),
            "valve_curve": self._valve_curve.to_persisted_dict(),
        }

    def reset_learning(self) -> None:
        """Reset all learned state and return to a fresh bootstrap runtime."""
        self._state = AdaptiveTPIState(
            k_int=self._default_kint,
            k_ext=self._default_kext,
        )
        self._valve_curve = build_valve_curve(
            self._actuator_mode,
            params=self._configured_valve_curve_params,
            compensation_enabled=self._valve_curve_compensation_enabled,
            learning_enabled=self._valve_curve_learning_enabled,
        )
        self._refresh_valve_curve_state()
        self._supervisor.reset()
        self._deadtime_model.reset()
        self._deadtime_tracker.reset()
        self._estimator.reset()
        self._state.a_hat = self._estimator.a_hat
        self._state.b_hat = self._estimator.b_hat
        self._state.c_a = self._estimator.c_a
        self._state.c_b = self._estimator.c_b
        self._last_accepted_at = None
        self._temperature_available = False
        self._pending_cycle_sample = None
        self._startup_bootstrap.reset()

    def reset_valve_curve(self) -> None:
        """Reset only the valve curve while keeping the learned 1R1C state."""
        self._valve_curve = build_valve_curve(
            self._actuator_mode,
            params=self._configured_valve_curve_params,
            compensation_enabled=self._valve_curve_compensation_enabled,
            learning_enabled=self._valve_curve_learning_enabled,
        )
        self._refresh_valve_curve_state()

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
            self._apply_persisted_time_units(data, current_cycle_min)
            self._last_accepted_at = self._parse_datetime(last_accepted_at)
            should_restore_deadtime = self._apply_persistence_invalidation(
                current_cycle_min=current_cycle_min,
                persisted_cycle_min=persisted_cycle_min,
                saved_at=saved_at,
                last_accepted_at=last_accepted_at,
            )
            deadtime_restored = False
            if should_restore_deadtime:
                deadtime_restored = self._deadtime_model.load_persisted_dict(
                    data.get("deadtime_model"),
                    cycle_min=current_cycle_min,
                )
            else:
                self._deadtime_model.reset()
                self._state.nd_hat = 0.0
                self._state.deadtime_minutes = None
                self._state.deadtime_on_cycles = None
                self._state.deadtime_on_minutes = None
                self._state.deadtime_on_confidence = 0.0
                self._state.deadtime_on_locked = False
                self._state.deadtime_off_cycles = None
                self._state.deadtime_off_minutes = None
                self._state.deadtime_off_confidence = 0.0
                self._state.deadtime_off_locked = False
                self._state.c_nd = 0.0
                self._state.deadtime_locked = False
                self._state.deadtime_best_candidate = None
                self._state.deadtime_second_best_candidate = None
                self._state.deadtime_b_proxy = None
                self._state.deadtime_identification_count = 0
                self._state.deadtime_identification_qualities = {}
                self._state.deadtime_pending_step = False
            estimator_restored = (
                should_restore_deadtime
                and self._estimator.load_persisted_dict(
                    data.get("estimator_model"),
                    cycle_min=current_cycle_min,
                )
            )
            if not estimator_restored:
                self._estimator.restore(
                    a_hat=self._state.a_hat,
                    b_hat=self._state.b_hat,
                    c_a=self._state.c_a,
                    c_b=self._state.c_b,
                )
            if not self._valve_curve.load_persisted_dict(data.get("valve_curve")):
                persisted_curve = data.get("valve_curve")
                if isinstance(persisted_curve, Mapping) and persisted_curve.get(
                    "actuator_mode"
                ):
                    self._supervisor.last_freeze_reason = "actuator_mode_changed"
            self._valve_curve.set_learning_enabled(self._valve_curve_learning_enabled)
            self._apply_configured_valve_curve_policy(data.get("valve_curve"))
            if deadtime_restored:
                deadtime_result = self._deadtime_model.last_result
                self._state.nd_hat = deadtime_result.nd_hat
                self._state.deadtime_minutes = deadtime_result.nd_minutes
                self._state.deadtime_on_cycles = (
                    deadtime_result.nd_hat_on
                    if deadtime_result.nd_hat_on is not None
                    else deadtime_result.nd_hat
                )
                self._state.deadtime_on_minutes = (
                    deadtime_result.nd_minutes_on
                    if deadtime_result.nd_minutes_on is not None
                    else deadtime_result.nd_minutes
                )
                self._state.deadtime_on_confidence = (
                    deadtime_result.c_nd_on or deadtime_result.c_nd
                )
                self._state.deadtime_on_locked = (
                    deadtime_result.deadtime_on_locked or deadtime_result.locked
                )
                self._state.deadtime_off_cycles = deadtime_result.nd_hat_off
                self._state.deadtime_off_minutes = deadtime_result.nd_minutes_off
                self._state.deadtime_off_confidence = deadtime_result.c_nd_off
                self._state.deadtime_off_locked = deadtime_result.deadtime_off_locked
                self._state.c_nd = deadtime_result.c_nd
                self._state.deadtime_locked = deadtime_result.locked
                self._state.deadtime_best_candidate = deadtime_result.best_candidate
                self._state.deadtime_second_best_candidate = deadtime_result.second_best_candidate
                self._state.deadtime_b_proxy = deadtime_result.best_candidate_b
                self._state.deadtime_identification_count = len(
                    self._deadtime_model._identifications
                ) + len(
                    self._deadtime_model._identifications_off
                )
                self._state.deadtime_identification_qualities = deadtime_result.candidate_costs
                self._state.deadtime_pending_step = self._deadtime_tracker.has_pending_step
            self._state.a_hat = self._estimator.a_hat
            self._state.b_hat = self._estimator.b_hat
            self._state.c_a = self._estimator.c_a
            self._state.c_b = self._estimator.c_b
            self._state.b_converged = self._estimator.b_converged
            self._state.a_samples_count = self._estimator._a_estimator.samples_count
            self._state.b_samples_count = self._estimator._b_estimator.samples_count
            self._state.a_last_reason = self._estimator._a_estimator.last_reason
            self._state.b_last_reason = self._estimator._b_estimator.last_reason
            self._state.a_dispersion = self._estimator._a_estimator.dispersion
            self._state.b_dispersion = self._estimator._b_estimator.dispersion
            self._supervisor.set_phase(self._state.bootstrap_phase)
            self._supervisor.apply_to_state(self._state)
            self._refresh_valve_curve_state()
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

    def _apply_estimator_update(self, estimator_update) -> None:
        """Copy one estimator snapshot into the public adaptive state."""
        self._state.a_hat = estimator_update.a_hat
        self._state.b_hat = estimator_update.b_hat
        self._state.c_a = estimator_update.c_a
        self._state.c_b = estimator_update.c_b
        self._state.b_converged = estimator_update.b_converged
        self._state.i_a = estimator_update.i_a
        self._state.i_b = estimator_update.i_b
        self._state.a_samples_count = estimator_update.a_samples_count
        self._state.b_samples_count = estimator_update.b_samples_count
        self._state.a_last_reason = estimator_update.a_last_reason
        self._state.b_last_reason = estimator_update.b_last_reason
        self._state.a_dispersion = estimator_update.a_dispersion
        self._state.b_dispersion = estimator_update.b_dispersion

    def _refresh_b_crosscheck(self) -> None:
        """Update diagnostics that compare the two independent `b` estimates."""
        proxy = self._state.deadtime_b_proxy
        if proxy is None or self._state.b_samples_count <= 0:
            self._state.b_crosscheck_error = None
            self._state.b_methods_consistent = False
            return

        scale = max(abs(proxy), abs(self._state.b_hat), 1e-6)
        error = abs(self._state.b_hat - proxy) / scale
        self._state.b_crosscheck_error = error
        self._state.b_methods_consistent = error <= _B_METHODS_CONSISTENT_THRESHOLD

    def observe_temperature_update(
        self,
        *,
        current_temp: float | None,
        target_temp: float | None,
        measured_at: datetime | None,
        hvac_mode,
    ) -> None:
        """Track deadtime from raw temperature updates while cycles remain active."""
        identification = self._deadtime_tracker.observe_temperature(
            measured_at=measured_at,
            current_temp=current_temp,
            target_temp=target_temp,
            mode_sign=hvac_mode_sign(hvac_mode),
        )
        self._state.deadtime_pending_step = self._deadtime_tracker.has_pending_step
        if identification is None:
            return
        deadtime_result = self._deadtime_model.record_identification(identification)
        self._apply_deadtime_result(deadtime_result)
        if (
            self._state.b_samples_count == 0
            and deadtime_result.best_candidate_b is not None
            and deadtime_result.c_nd >= _DEADTIME_B_PROXY_SEED_CONFIDENCE
        ):
            self._apply_estimator_update(
                self._estimator.seed_b_from_deadtime_proxy(deadtime_result.best_candidate_b)
            )
            self._refresh_b_crosscheck()

    def _apply_deadtime_result(self, deadtime_result) -> None:
        """Mirror one deadtime result into the public adaptive state."""
        self._state.nd_hat = deadtime_result.nd_hat
        self._state.deadtime_minutes = deadtime_result.nd_minutes
        self._state.deadtime_on_cycles = (
            deadtime_result.nd_hat_on
            if deadtime_result.nd_hat_on is not None
            else deadtime_result.nd_hat
        )
        self._state.deadtime_on_minutes = (
            deadtime_result.nd_minutes_on
            if deadtime_result.nd_minutes_on is not None
            else deadtime_result.nd_minutes
        )
        self._state.deadtime_on_confidence = (
            deadtime_result.c_nd_on or deadtime_result.c_nd
        )
        self._state.deadtime_on_locked = (
            deadtime_result.deadtime_on_locked or deadtime_result.locked
        )
        self._state.deadtime_off_cycles = deadtime_result.nd_hat_off
        self._state.deadtime_off_minutes = deadtime_result.nd_minutes_off
        self._state.deadtime_off_confidence = deadtime_result.c_nd_off
        self._state.deadtime_off_locked = deadtime_result.deadtime_off_locked
        self._state.c_nd = deadtime_result.c_nd
        self._state.deadtime_locked = deadtime_result.locked
        self._state.deadtime_best_candidate = deadtime_result.best_candidate
        self._state.deadtime_second_best_candidate = deadtime_result.second_best_candidate
        self._state.deadtime_b_proxy = deadtime_result.best_candidate_b
        self._state.deadtime_identification_count = len(
            self._deadtime_model._identifications
        ) + len(self._deadtime_model._identifications_off)
        self._state.deadtime_identification_qualities = deadtime_result.candidate_costs
        self._state.deadtime_pending_step = self._deadtime_tracker.has_pending_step
        self._supervisor.apply_deadtime_result(
            locked=deadtime_result.locked,
            confidence=deadtime_result.c_nd,
            lock_reason=deadtime_result.lock_reason,
        )
        self._state.a_learning_enabled = self._supervisor.is_a_learning_enabled(
            deadtime_locked=deadtime_result.locked,
            c_nd=deadtime_result.c_nd,
            b_converged=self._state.b_converged,
        )
        self._supervisor.apply_to_state(self._state)

    @property
    def on_percent(self) -> float | None:
        """Return the currently applied heating fraction."""
        if not self._temperature_available:
            return None
        return self._state.requested_on_percent

    @property
    def requested_on_percent(self) -> float | None:
        """Return the requested heating fraction for the next cycle."""
        if not self._temperature_available:
            return None
        return self._state.requested_on_percent

    @property
    def startup_bootstrap_command_on_percent(self) -> float | None:
        """Return the current bootstrap override command when startup bootstrap is active."""
        return self._state.startup_bootstrap_command_on_percent

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
    ) -> bool:
        """Adjust persisted trust depending on runtime context changes.

        Returns True when deadtime history may safely be restored.
        """
        del persisted_cycle_min
        if isinstance(current_cycle_min, (int, float)) and current_cycle_min > 0.0:
            self._state.cycle_min_at_last_accepted_cycle = float(current_cycle_min)

        reference_time = self._parse_datetime(last_accepted_at) or self._parse_datetime(
            saved_at
        )
        if reference_time is None:
            return True

        age_days = (self._utc_now() - reference_time).days
        if age_days > _CONFIDENCE_DECAY_30_DAYS:
            self._state.decay_confidences(0.5)
            if not self._state.deadtime_locked:
                self._state.bootstrap_phase = PHASE_B
                self._supervisor.set_phase(PHASE_B)
            self._supervisor.last_freeze_reason = "warm_start_confidence_decay"
            self._state.last_freeze_reason = self._supervisor.last_freeze_reason
        return True

    def _apply_persisted_time_units(
        self,
        data: Mapping[str, Any],
        current_cycle_min: float | None,
    ) -> None:
        """Convert persisted time-canonical values into runtime cycle units."""
        if not isinstance(current_cycle_min, (int, float)) or current_cycle_min <= 0.0:
            return
        cycle_min = float(current_cycle_min)

        a_hat_per_hour = self._mapping_float(data, "a_hat_per_hour")
        if a_hat_per_hour is not None:
            self._state.a_hat = max(1e-3, a_hat_per_hour * cycle_min / 60.0)

        b_hat_per_hour = self._mapping_float(data, "b_hat_per_hour")
        if b_hat_per_hour is not None:
            self._state.b_hat = max(0.0, b_hat_per_hour * cycle_min / 60.0)

        if self._state.deadtime_minutes is not None:
            self._state.nd_hat = self._state.deadtime_minutes / cycle_min
        if self._state.deadtime_on_minutes is not None:
            self._state.deadtime_on_cycles = (
                self._state.deadtime_on_minutes / cycle_min
            )
        if self._state.deadtime_off_minutes is not None:
            self._state.deadtime_off_cycles = (
                self._state.deadtime_off_minutes / cycle_min
            )

        best_candidate_minutes = self._mapping_float(
            data,
            "deadtime_best_candidate_minutes",
        )
        if best_candidate_minutes is not None:
            self._state.deadtime_best_candidate = best_candidate_minutes / cycle_min

        second_best_candidate_minutes = self._mapping_float(
            data,
            "deadtime_second_best_candidate_minutes",
        )
        if second_best_candidate_minutes is not None:
            self._state.deadtime_second_best_candidate = (
                second_best_candidate_minutes / cycle_min
            )

        deadtime_b_proxy_per_hour = self._mapping_float(
            data,
            "deadtime_b_proxy_per_hour",
        )
        if deadtime_b_proxy_per_hour is not None:
            self._state.deadtime_b_proxy = deadtime_b_proxy_per_hour * cycle_min / 60.0

    @staticmethod
    def _mapping_float(data: Mapping[str, Any], key: str) -> float | None:
        """Return one numeric mapping value as float."""
        value = data.get(key)
        if value is None or isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

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
        """Return True when thermal excitation is sufficient for deadtime learning."""
        outdoor_delta = abs(sample.current_temp - sample.outdoor_temp)
        return outdoor_delta >= _MIN_DEADTIME_OUTDOOR_DELTA

    @staticmethod
    def _is_estimator_informative_cycle(sample: CycleSample) -> bool:
        """Return True when one cycle is informative for `b` or `a` estimation.

        OFF cycles skip the setpoint-error gate because b = -dTdt/delta_out does
        not require a gap from setpoint to be physically meaningful.
        """
        outdoor_delta = abs(sample.current_temp - sample.outdoor_temp)
        if outdoor_delta < _MIN_ESTIMATOR_OUTDOOR_DELTA:
            return False
        regime = classify_cycle_regime(sample.applied_demand)
        if regime == WINDOW_REGIME_OFF:
            return True
        setpoint_error = abs(sample.target_temp - sample.current_temp)
        return setpoint_error >= _MIN_ESTIMATOR_SETPOINT_ERROR

    def _increment_hours_without_excitation(self, cycle_duration_min: float | None) -> None:
        """Track how long learning has been starved of informative cycles."""
        if isinstance(cycle_duration_min, (int, float)) and cycle_duration_min > 0:
            self._state.hours_without_excitation += float(cycle_duration_min) / 60.0

    def _make_deadtime_observation(self, sample: CycleSample) -> DeadtimeObservation:
        """Convert one committed scheduler cycle into a learning observation."""
        return DeadtimeObservation(
            tin=sample.current_temp,
            tout=sample.outdoor_temp,
            target_temp=sample.target_temp,
            applied_power=sample.applied_power,
            applied_demand=sample.applied_demand,
        )

    def _learning_observations_with_terminal(
        self,
        *,
        target_temp: float | None,
        current_temp: float | None,
        ext_current_temp: float | None,
        cycle_duration_min: float,
        applied_power: float,
        applied_demand: float,
    ) -> tuple[CycleHistoryEntry, ...]:
        """Return the stored cycle history plus the current cycle end point."""
        if target_temp is None or current_temp is None or ext_current_temp is None:
            return self._deadtime_model.cycle_history
        terminal_entry = CycleHistoryEntry(
            tin=float(current_temp),
            tout=float(ext_current_temp),
            target_temp=float(target_temp),
            applied_power=float(applied_power),
            applied_demand=float(applied_demand),
            is_valid=True,
            is_informative=False,
            is_estimator_informative=False,
            cycle_duration_min=cycle_duration_min,
        )
        return self._deadtime_model.cycle_history + (terminal_entry,)

    def _record_cycle_history(
        self,
        sample: CycleSample,
        *,
        cycle_duration_min: float = 5.0,
        is_valid: bool,
        is_informative: bool,
        is_estimator_informative: bool,
    ) -> None:
        """Append one real cycle to the temporal history used by identification."""
        self._deadtime_model.record_cycle(
            self._make_deadtime_observation(sample),
            cycle_duration_min=cycle_duration_min,
            is_valid=is_valid,
            is_informative=is_informative,
            is_estimator_informative=is_estimator_informative,
            bootstrap_b_learning_allowed=sample.bootstrap_b_learning_allowed,
            mode_sign=hvac_mode_sign(sample.hvac_mode),
            track_step_response=False,
        )

    def _refresh_valve_curve_state(self) -> None:
        """Copy actuator linearization status into diagnostics state."""
        self._state.actuator_mode = self._actuator_mode
        params = self._valve_curve.params
        self._state.valve_curve_params = (
            None
            if params is None
            else {
                "min_valve": params.min_valve,
                "knee_demand": params.knee_demand,
                "knee_valve": params.knee_valve,
                "max_valve": params.max_valve,
            }
        )
        self._state.valve_curve_learning_enabled = self._valve_curve.learning_enabled
        self._state.valve_curve_converged = self._valve_curve.is_converged
        self._state.valve_curve_observations_accepted = (
            self._valve_curve.observations_accepted_count
        )
        self._state.valve_curve_observations_rejected = (
            self._valve_curve.observations_rejected_count
        )
        self._state.valve_curve_rejected_updates = self._valve_curve.rejected_updates
        self._state.valve_curve_last_reason = self._valve_curve.last_reason

    def _apply_configured_valve_curve_policy(
        self,
        persisted_curve: Mapping[str, Any] | None,
    ) -> None:
        """Keep configured manual valve parameters authoritative when required."""
        if (
            self._actuator_mode != ACTUATOR_MODE_VALVE
            or self._configured_valve_curve_params is None
        ):
            return
        configured_params = self._configured_valve_curve_params
        current_config = asdict(configured_params)
        persisted_config = (
            persisted_curve.get("configured_params")
            if isinstance(persisted_curve, Mapping)
            else None
        )
        config_matches_persisted = (
            isinstance(persisted_config, Mapping)
            and persisted_config == current_config
        )
        if not self._valve_curve_learning_enabled or not config_matches_persisted:
            self._valve_curve.set_params(configured_params)
            self._valve_curve.reset_learning()
        if hasattr(self._valve_curve, "set_configured_params"):
            self._valve_curve.set_configured_params(configured_params)

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
            tau_cl_min=self._tau_cl_min,
            default_kint=self._default_kint,
            default_kext=self._default_kext,
        )

    def _apply_startup_bootstrap_snapshot(self, snapshot: StartupBootstrapSnapshot) -> None:
        """Copy the startup bootstrap status into the public runtime state."""
        self._state.startup_bootstrap_active = snapshot.active
        self._state.startup_bootstrap_stage = snapshot.stage
        self._state.startup_bootstrap_attempt = snapshot.attempt
        self._state.startup_bootstrap_max_attempts = snapshot.max_attempts
        self._state.startup_bootstrap_target_temp = snapshot.target_temp
        self._state.startup_bootstrap_lower_target_temp = snapshot.lower_target_temp
        self._state.startup_bootstrap_upper_target_temp = snapshot.upper_target_temp
        self._state.startup_bootstrap_command_on_percent = snapshot.command_on_percent
        self._state.startup_bootstrap_completion_reason = snapshot.completion_reason

    def should_force_bootstrap_cycle_restart(
        self,
        *,
        target_temp: float | None,
        current_temp: float | None,
        hvac_mode,
    ) -> bool:
        """Return True when bootstrap threshold crossing should force a new cycle."""
        sign = hvac_mode_sign(hvac_mode)
        return self._startup_bootstrap.should_force_cycle_restart(
            target_temp=target_temp,
            current_temp=current_temp,
            deadtime_identification_count=self._state.deadtime_identification_count,
            deadtime_on_locked=self._state.deadtime_on_locked,
            deadtime_off_locked=self._state.deadtime_off_locked,
            heating_enabled=sign != 0,
            mode_sign=sign,
        )

    def should_force_bootstrap_cycle_restart_after_calculation(
        self,
        *,
        previous_requested_on_percent: float | None,
        previous_bootstrap_command_on_percent: float | None,
    ) -> bool:
        """Return True when a bootstrap override changed the command mid-cycle."""
        current_requested_on_percent = self._state.requested_on_percent
        current_bootstrap_command_on_percent = (
            self._state.startup_bootstrap_command_on_percent
        )
        if current_requested_on_percent is None:
            return False
        if (
            current_bootstrap_command_on_percent is not None
            and abs(current_requested_on_percent - self._state.committed_on_percent) > 1e-9
        ):
            return True
        if (
            previous_requested_on_percent is None
            or abs(current_requested_on_percent - previous_requested_on_percent) <= 1e-9
        ):
            return False
        return (
            previous_bootstrap_command_on_percent is not None
            or current_bootstrap_command_on_percent is not None
        )
