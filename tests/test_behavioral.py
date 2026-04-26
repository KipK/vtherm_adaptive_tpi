"""Behavioral tests for the Adaptive TPI implementation roadmap."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.vtherm_adaptive_tpi.algo import AdaptiveTPIAlgorithm
from custom_components.vtherm_adaptive_tpi.const import (
    ACTUATOR_MODE_AUTO,
    ACTUATOR_MODE_SWITCH,
    ACTUATOR_MODE_VALVE,
    CONF_VALVE_KNEE_DEMAND,
    CONF_VALVE_KNEE_VALVE,
    CONF_VALVE_MAX_VALVE,
    CONF_VALVE_MIN_VALVE,
)
from custom_components.vtherm_adaptive_tpi.config_flow import (
    ERROR_INVALID_VALVE_CURVE,
    _validate_valve_curve_config,
)
from custom_components.vtherm_adaptive_tpi.adaptive_tpi.controller import (
    compute_gain_targets,
    project_gains,
)
from custom_components.vtherm_adaptive_tpi.handler import (
    AdaptiveTPIHandler,
    _AdaptiveTPIStore,
    _resolve_actuator_mode,
    _resolve_effective_actuator_mode,
)
from custom_components.vtherm_adaptive_tpi.adaptive_tpi.state import (
    PERSISTENCE_SCHEMA_VERSION,
)
from custom_components.vtherm_adaptive_tpi.adaptive_tpi.startup_bootstrap import (
    STARTUP_BOOTSTRAP_FINAL_COOLDOWN,
    StartupBootstrapController,
)
from custom_components.vtherm_adaptive_tpi.adaptive_tpi.valve_curve import (
    IdentityValveCurve,
    TwoSlopeValveCurve,
    VALVE_CURVE_DEFAULTS,
    ValveCurveParams,
)
from custom_components.vtherm_adaptive_tpi.adaptive_tpi.deadtime import (
    CONFIDENCE_LOCK_THRESHOLD,
    N_MAX_RISE_CYCLES,
    CycleHistoryEntry,
    DeadtimeModel,
    DeadtimeObservation,
    DeadtimeSearchResult,
    StepIdentification,
    _compute_b_proxy,
    _find_latest_step,
    _measure_rise_delay,
)
from custom_components.vtherm_adaptive_tpi.adaptive_tpi.estimator import (
    ASample,
    A_MAX,
    A_MIN,
    BSample,
    B_MAX,
    B_MIN,
    MAD_OUTLIER_MIN_SAMPLES,
    OUTLIER_REGIME_CONFIRMATION_COUNT,
    ParameterEstimator,
)
from custom_components.vtherm_adaptive_tpi.adaptive_tpi.learning_window import (
    WINDOW_REGIME_MIXED,
    WINDOW_REGIME_OFF,
    WINDOW_REGIME_ON,
    build_anchored_learning_window,
    build_learning_window,
    classify_cycle_regime,
)
from custom_components.vtherm_adaptive_tpi.adaptive_tpi.supervisor import (
    PHASE_A,
    PHASE_B,
    PHASE_C,
    PHASE_D,
)


def _make_step_observations(
    nd: int = 2,
    n_off: int = 4,
    n_on: int = 15,
) -> tuple[CycleHistoryEntry, ...]:
    """Build a synthetic step-response sequence with the given deadtime in cycles.

    a=2.0, b=0.5, tout=4.9, tin_0=4.92 → equilibrium ~8.1°C, plateau in ~11 ON cycles.
    """
    a = 2.0
    b = 0.5
    tout = 4.9
    target = 15.0
    tin = 4.92
    entries: list[CycleHistoryEntry] = []
    all_powers: list[float] = []

    for k in range(n_off + n_on):
        power = 0.0 if k < n_off else 0.8
        delayed_k = k - nd
        delayed_power = all_powers[delayed_k] if delayed_k >= 0 else 0.0
        entries.append(
            CycleHistoryEntry(
                tin=tin,
                tout=tout,
                target_temp=target,
                applied_power=power,
                is_valid=True,
                is_informative=True,
                is_estimator_informative=True,
                cycle_duration_min=5.0,
            )
        )
        all_powers.append(power)
        tin = tin + a * delayed_power - b * (tin - tout)

    return tuple(entries)


def test_resolve_actuator_mode_from_entry_infos() -> None:
    """Actuator mode follows VTherm metadata."""
    assert _resolve_actuator_mode(None) == ACTUATOR_MODE_SWITCH
    assert (
        _resolve_actuator_mode({"thermostat_type": "thermostat_over_switch"})
        == ACTUATOR_MODE_SWITCH
    )
    assert (
        _resolve_actuator_mode({"thermostat_type": "thermostat_over_valve"})
        == ACTUATOR_MODE_VALVE
    )
    assert (
        _resolve_actuator_mode(
            {
                "thermostat_type": "thermostat_over_climate",
                "auto_regulation_mode": "auto_regulation_valve",
            }
        )
        == ACTUATOR_MODE_VALVE
    )


def test_resolve_effective_actuator_mode_prefers_override() -> None:
    """A configured override must take precedence over auto-detection."""
    assert (
        _resolve_effective_actuator_mode(
            {"actuator_mode_override": ACTUATOR_MODE_SWITCH},
            {"thermostat_type": "thermostat_over_valve"},
        )
        == ACTUATOR_MODE_SWITCH
    )
    assert (
        _resolve_effective_actuator_mode(
            {"actuator_mode_override": ACTUATOR_MODE_AUTO},
            {"thermostat_type": "thermostat_over_valve"},
        )
        == ACTUATOR_MODE_VALVE
    )


def test_validate_valve_curve_config_rejects_invalid_breakpoint_order() -> None:
    """Valve curve form validation should reject ambiguous breakpoint ordering."""
    assert (
        _validate_valve_curve_config(
            {
                CONF_VALVE_MIN_VALVE: 7.0,
                CONF_VALVE_KNEE_DEMAND: 80.0,
                CONF_VALVE_KNEE_VALVE: 15.0,
                CONF_VALVE_MAX_VALVE: 100.0,
            }
        )
        == {}
    )

    errors = _validate_valve_curve_config(
        {
            CONF_VALVE_MIN_VALVE: 20.0,
            CONF_VALVE_KNEE_DEMAND: 80.0,
            CONF_VALVE_KNEE_VALVE: 10.0,
            CONF_VALVE_MAX_VALVE: 100.0,
        }
    )

    assert errors == {"base": ERROR_INVALID_VALVE_CURVE}


def test_identity_valve_curve_is_strictly_linear() -> None:
    """Switch actuators keep the existing linear command space."""
    curve = IdentityValveCurve()
    for value in (-0.5, 0.0, 0.42, 1.0, 1.5):
        expected = min(1.0, max(0.0, value))
        assert curve.apply(value) == pytest.approx(expected)
        assert curve.invert(value) == pytest.approx(expected)


def test_two_slope_valve_curve_round_trips_reachable_points() -> None:
    """Reachable valve positions invert back to the model demand."""
    curve = TwoSlopeValveCurve(VALVE_CURVE_DEFAULTS)
    assert curve.apply(0.0) == pytest.approx(0.0)
    assert curve.apply(0.06) == pytest.approx(0.076)
    assert curve.apply(0.07) == pytest.approx(0.077)
    assert curve.apply(0.80) == pytest.approx(0.15)
    assert curve.apply(1.0) == pytest.approx(1.0)
    for demand in (0.07, 0.20, 0.80, 0.95, 1.0):
        assert curve.invert(curve.apply(demand)) == pytest.approx(demand)


def test_two_slope_valve_curve_rejects_invalid_parameters() -> None:
    """Invalid curve breakpoints must not be accepted."""
    with pytest.raises(ValueError):
        ValveCurveParams(
            min_valve=15.0,
            knee_demand=80.0,
            knee_valve=7.0,
            max_valve=100.0,
        )


def test_two_slope_valve_curve_learning_converges_toward_observed_curve() -> None:
    """Bounded online learning should move the curve toward a consistent valve response."""
    learned_curve = TwoSlopeValveCurve(VALVE_CURVE_DEFAULTS, learning_enabled=True)
    true_curve = TwoSlopeValveCurve(
        ValveCurveParams(
            min_valve=10.0,
            knee_demand=70.0,
            knee_valve=25.0,
            max_valve=92.0,
        ),
        learning_enabled=True,
    )
    a_hat = 0.4
    b_hat = 0.02
    delta_out = 5.0

    for demand in (
        0.12,
        0.18,
        0.24,
        0.30,
        0.36,
        0.42,
        0.48,
        0.54,
        0.60,
        0.66,
        0.72,
        0.78,
        0.84,
        0.90,
        0.96,
        0.20,
        0.40,
        0.62,
        0.74,
        0.88,
    ):
        learned_curve.observe(
            u_valve=true_curve.apply(demand),
            dTdt=a_hat * demand - b_hat * delta_out,
            delta_out=delta_out,
            a_hat=a_hat,
            b_hat=b_hat,
            b_converged=True,
            mode_sign=1,
        )

    params = learned_curve.params
    assert learned_curve.observations_accepted_count == 20
    assert learned_curve.observations_rejected_count == 0
    assert learned_curve.is_converged is True
    assert 1.0 <= params.min_valve < params.knee_valve < params.max_valve <= 100.0
    assert 0.0 < params.knee_demand < 100.0
    for demand in (0.20, 0.40, 0.80, 0.95):
        assert learned_curve.apply(demand) == pytest.approx(
            true_curve.apply(demand),
            abs=0.06,
        )


def test_two_slope_valve_curve_persists_learning_history_and_counters() -> None:
    """Curve persistence should keep observations and diagnostics counters."""
    curve = TwoSlopeValveCurve(VALVE_CURVE_DEFAULTS, learning_enabled=True)
    curve.observe(
        u_valve=0.20,
        dTdt=0.10,
        delta_out=5.0,
        a_hat=0.4,
        b_hat=0.02,
        b_converged=True,
        mode_sign=1,
        timestamp="2026-04-24T12:00:00+00:00",
    )

    restored = TwoSlopeValveCurve(VALVE_CURVE_DEFAULTS, learning_enabled=False)
    assert restored.load_persisted_dict(curve.to_persisted_dict()) is True

    assert restored.learning_enabled is True
    assert restored.observations_accepted_count == 1
    assert restored.observations_rejected_count == 0
    assert restored.last_reason == "sample_accepted"


def test_two_slope_valve_curve_cooling_formula_matches_estimator_signs() -> None:
    """Cooling observations should use the same signed power proxy as the estimator."""
    curve = TwoSlopeValveCurve(VALVE_CURVE_DEFAULTS, learning_enabled=True)
    curve.observe(
        u_valve=0.20,
        dTdt=-0.20,
        delta_out=5.0,
        a_hat=0.4,
        b_hat=0.02,
        b_converged=True,
        mode_sign=-1,
    )

    assert curve.observations_accepted_count == 1
    assert curve.last_reason == "sample_accepted"
    persisted = curve.to_persisted_dict()
    assert persisted["observations"][0]["u_linear_equiv"] == pytest.approx(0.25)


def test_switch_mode_keeps_runtime_command_identity() -> None:
    """Switch mode must keep identical linear and requested commands."""
    algo = AdaptiveTPIAlgorithm(
        name="test-switch-command-identity",
        actuator_mode=ACTUATOR_MODE_SWITCH,
    )
    algo.calculate(
        target_temp=20.0,
        current_temp=19.5,
        ext_current_temp=10.0,
        slope=None,
        hvac_mode="heat",
    )
    assert algo.requested_on_percent == pytest.approx(algo.calculated_on_percent)


def test_valve_mode_applies_curve_to_requested_command() -> None:
    """Valve mode must expose actuator command in the valve position space."""
    algo = AdaptiveTPIAlgorithm(
        name="test-valve-command-curve",
        actuator_mode=ACTUATOR_MODE_VALVE,
    )
    algo._state.deadtime_identification_count = 1
    algo._state.deadtime_on_locked = True
    algo._state.deadtime_off_locked = True
    algo.calculate(
        target_temp=20.0,
        current_temp=19.5,
        ext_current_temp=10.0,
        slope=None,
        hvac_mode="heat",
    )
    expected_requested = TwoSlopeValveCurve(VALVE_CURVE_DEFAULTS).apply(
        algo.calculated_on_percent
    )
    assert algo.calculated_on_percent == pytest.approx(0.5)
    assert algo.requested_on_percent == pytest.approx(expected_requested)
    assert algo.requested_on_percent != pytest.approx(algo.calculated_on_percent)


def test_valve_mode_cycle_samples_store_linearized_applied_demand() -> None:
    """Valve mode cycle samples must keep actuator power and model demand separate."""
    algo = AdaptiveTPIAlgorithm(
        name="test-valve-cycle-demand",
        actuator_mode=ACTUATOR_MODE_VALVE,
    )

    algo.on_cycle_started(
        on_time_sec=180.0,
        off_time_sec=120.0,
        on_percent=0.15,
        hvac_mode="heat",
        target_temp=21.0,
        current_temp=20.0,
        ext_current_temp=10.0,
    )

    pending = algo._pending_cycle_sample
    assert pending is not None
    assert pending.applied_power == pytest.approx(0.15)
    assert pending.applied_demand == pytest.approx(0.80)

    completed = algo._resolve_completed_cycle_sample(pending, 0.101)
    assert completed.applied_power == pytest.approx(0.101)
    assert completed.applied_demand == pytest.approx(0.31)


def test_valve_curve_learning_accepts_stable_mixed_cycles_without_a_update() -> None:
    """Mixed valve cycles should feed curve learning without opening A learning."""
    algo = AdaptiveTPIAlgorithm(
        name="test-valve-curve-mixed-learning",
        actuator_mode=ACTUATOR_MODE_VALVE,
        debug_mode=True,
    )
    algo._state.a_hat = 0.4
    algo._state.b_hat = 0.001
    algo._state.b_converged = True
    mixed_valve_position = algo._valve_curve.apply(0.18)

    algo.on_cycle_started(
        on_time_sec=60.0,
        off_time_sec=240.0,
        on_percent=mixed_valve_position,
        hvac_mode="heat",
        target_temp=21.0,
        current_temp=20.0,
        ext_current_temp=10.0,
    )
    algo.on_cycle_completed(
        elapsed_ratio=1.0,
        cycle_duration_min=5.0,
        measure_timestamp=datetime(2026, 4, 24, tzinfo=timezone.utc),
        target_temp=21.0,
        current_temp=20.05,
        ext_current_temp=10.0,
        hvac_mode="heat",
    )

    algo.on_cycle_started(
        on_time_sec=60.0,
        off_time_sec=240.0,
        on_percent=mixed_valve_position,
        hvac_mode="heat",
        target_temp=21.0,
        current_temp=20.05,
        ext_current_temp=10.0,
    )
    algo.on_cycle_completed(
        elapsed_ratio=1.0,
        cycle_duration_min=5.0,
        measure_timestamp=datetime(2026, 4, 24, 0, 5, tzinfo=timezone.utc),
        target_temp=21.0,
        current_temp=20.12,
        ext_current_temp=10.0,
        hvac_mode="heat",
    )

    diagnostics = algo.get_diagnostics()
    assert diagnostics["debug"]["current_cycle_regime"] == WINDOW_REGIME_MIXED
    assert diagnostics["debug"]["learning_route_selected"] == "none"
    assert diagnostics["last_learning_result"] == "mixed_cycle_regime"
    assert diagnostics["valve_curve_observations_accepted"] == 1
    assert diagnostics["valve_curve_last_reason"] == "sample_accepted"
    assert diagnostics["control_samples"] == 0


def test_startup_with_no_history_keeps_bootstrap_defaults() -> None:
    """Fresh runtime should expose deterministic startup diagnostics."""
    algo = AdaptiveTPIAlgorithm(name="test-startup")

    diagnostics = algo.get_diagnostics()

    assert diagnostics["adaptive_phase"] == "startup"
    assert diagnostics["gain_indoor"] == pytest.approx(0.6)
    assert diagnostics["gain_outdoor"] == pytest.approx(0.02)
    assert diagnostics["deadtime_minutes"] is None
    assert diagnostics["startup_sequence_active"] is False
    assert diagnostics["startup_sequence_stage"] == "idle"
    assert diagnostics["startup_sequence_attempt"] == 0
    assert diagnostics["startup_sequence_max_attempts"] == 0
    assert diagnostics["control_rate_per_hour"] is None
    assert diagnostics["drift_rate_per_hour"] is None
    assert diagnostics["thermal_time_constant_hours"] is None
    assert diagnostics["control_rate_converged"] is False


def test_temporal_deadtime_identification_survives_interrupted_cycle() -> None:
    """A raw-temperature onset should identify deadtime even if the cycle is interrupted."""
    algo = AdaptiveTPIAlgorithm(name="test-temporal-deadtime")
    step_started_at = datetime(2026, 4, 22, 12, 57, 3, tzinfo=timezone.utc)
    algo._utc_now = lambda: step_started_at

    algo.on_cycle_started(
        on_time_sec=300.0,
        off_time_sec=0.0,
        on_percent=1.0,
        hvac_mode="heat",
        target_temp=25.0,
        current_temp=24.57,
        ext_current_temp=17.48,
    )

    algo.observe_temperature_update(
        current_temp=24.563,
        target_temp=25.0,
        measured_at=step_started_at + timedelta(seconds=30),
        hvac_mode="heat",
    )
    assert algo.get_diagnostics()["deadtime_minutes"] is None

    algo.observe_temperature_update(
        current_temp=24.596,
        target_temp=25.0,
        measured_at=step_started_at + timedelta(seconds=60),
        hvac_mode="heat",
    )
    algo.observe_temperature_update(
        current_temp=24.632,
        target_temp=25.0,
        measured_at=step_started_at + timedelta(seconds=90),
        hvac_mode="heat",
    )
    algo.observe_temperature_update(
        current_temp=24.681,
        target_temp=25.0,
        measured_at=step_started_at + timedelta(seconds=120),
        hvac_mode="heat",
    )

    diagnostics = algo.get_diagnostics()
    assert diagnostics["deadtime_minutes"] == pytest.approx(1.0)
    assert diagnostics["deadtime_cycles"] == pytest.approx(0.2)

    algo.on_cycle_completed(
        elapsed_ratio=0.85,
        cycle_duration_min=5.0,
        measure_timestamp=step_started_at + timedelta(seconds=150),
        target_temp=25.0,
        current_temp=25.0,
        ext_current_temp=17.48,
        hvac_mode="heat",
    )

    diagnostics = algo.get_diagnostics()
    assert diagnostics["deadtime_minutes"] == pytest.approx(1.0)
    assert diagnostics["deadtime_cycles"] == pytest.approx(0.2)
    assert diagnostics["last_learning_result"] == "cycle_interrupted"


def test_startup_bootstrap_cools_down_immediately_when_temperature_is_at_setpoint() -> None:
    """Startup bootstrap should begin with an OFF cooldown when already at setpoint."""
    algo = AdaptiveTPIAlgorithm(name="test-startup-bootstrap-cooldown")

    algo.calculate(
        target_temp=20.0,
        current_temp=20.0,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )

    diagnostics = algo.get_diagnostics()
    assert algo.requested_on_percent == pytest.approx(0.0)
    assert algo._state.committed_on_percent == pytest.approx(0.0)
    assert diagnostics["startup_sequence_active"] is True
    assert diagnostics["startup_sequence_stage"] == "passive_drift_phase"
    assert diagnostics["startup_sequence_attempt"] == 1


def test_startup_bootstrap_retries_deadtime_cycle_when_one_family_is_missing() -> None:
    """Startup bootstrap should repeat the full cycle while a deadtime family is missing."""
    algo = AdaptiveTPIAlgorithm(name="test-startup-bootstrap-retry")

    algo.calculate(
        target_temp=20.0,
        current_temp=19.7,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )
    diagnostics = algo.get_diagnostics()
    assert algo.requested_on_percent == pytest.approx(0.0)
    assert algo._state.committed_on_percent == pytest.approx(0.0)
    assert diagnostics["startup_sequence_stage"] == "passive_drift_phase"

    algo.calculate(
        target_temp=20.0,
        current_temp=19.5,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )
    diagnostics = algo.get_diagnostics()
    assert algo.requested_on_percent == pytest.approx(1.0)
    assert algo._state.committed_on_percent == pytest.approx(0.0)
    assert diagnostics["startup_sequence_stage"] == "reactivation_to_upper_target"

    algo.calculate(
        target_temp=20.0,
        current_temp=20.5,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )
    diagnostics = algo.get_diagnostics()
    assert algo.requested_on_percent == pytest.approx(0.0)
    assert algo._state.committed_on_percent == pytest.approx(0.0)
    assert diagnostics["startup_sequence_stage"] == "return_to_target"

    algo.calculate(
        target_temp=20.0,
        current_temp=20.0,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )

    diagnostics = algo.get_diagnostics()
    assert algo.requested_on_percent == pytest.approx(0.0)
    assert algo._state.committed_on_percent == pytest.approx(0.0)
    assert diagnostics["startup_sequence_active"] is True
    assert diagnostics["startup_sequence_stage"] == "passive_drift_phase"
    assert diagnostics["startup_sequence_attempt"] == 2
    assert diagnostics["startup_sequence_completion_reason"] == "deadtime_on_off_retry"


def test_startup_bootstrap_retries_deadtime_cycle_in_cool_mode() -> None:
    """Startup bootstrap should mirror the ON/OFF retry sequence when cooling is active."""
    algo = AdaptiveTPIAlgorithm(name="test-startup-bootstrap-retry-cool")

    algo.calculate(
        target_temp=25.0,
        current_temp=25.3,
        ext_current_temp=30.0,
        slope=None,
        hvac_mode="cool",
    )
    diagnostics = algo.get_diagnostics()
    assert algo.requested_on_percent == pytest.approx(0.0)
    assert algo._state.committed_on_percent == pytest.approx(0.0)
    assert diagnostics["startup_sequence_stage"] == "passive_drift_phase"

    algo.calculate(
        target_temp=25.0,
        current_temp=25.5,
        ext_current_temp=30.0,
        slope=None,
        hvac_mode="cool",
    )
    diagnostics = algo.get_diagnostics()
    assert algo.requested_on_percent == pytest.approx(1.0)
    assert algo._state.committed_on_percent == pytest.approx(0.0)
    assert diagnostics["startup_sequence_stage"] == "reactivation_to_upper_target"

    algo.calculate(
        target_temp=25.0,
        current_temp=24.5,
        ext_current_temp=30.0,
        slope=None,
        hvac_mode="cool",
    )
    diagnostics = algo.get_diagnostics()
    assert algo.requested_on_percent == pytest.approx(0.0)
    assert algo._state.committed_on_percent == pytest.approx(0.0)
    assert diagnostics["startup_sequence_stage"] == "return_to_target"

    algo.calculate(
        target_temp=25.0,
        current_temp=25.0,
        ext_current_temp=30.0,
        slope=None,
        hvac_mode="cool",
    )

    diagnostics = algo.get_diagnostics()
    assert algo.requested_on_percent == pytest.approx(0.0)
    assert algo._state.committed_on_percent == pytest.approx(0.0)
    assert diagnostics["startup_sequence_active"] is True
    assert diagnostics["startup_sequence_stage"] == "passive_drift_phase"
    assert diagnostics["startup_sequence_attempt"] == 2
    assert diagnostics["startup_sequence_completion_reason"] == "deadtime_on_off_retry"


def test_startup_bootstrap_returns_to_target_after_on_and_off_deadtime_cycle() -> None:
    """Startup bootstrap should drift back to target after both deadtime families lock."""
    algo = AdaptiveTPIAlgorithm(name="test-startup-bootstrap-complete")

    algo.calculate(
        target_temp=20.0,
        current_temp=20.0,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )
    algo.calculate(
        target_temp=20.0,
        current_temp=19.5,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )
    algo._state.deadtime_identification_count = 1
    algo._state.deadtime_on_locked = True
    algo._state.deadtime_off_locked = True
    algo.calculate(
        target_temp=20.0,
        current_temp=20.5,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )

    diagnostics = algo.get_diagnostics()
    assert diagnostics["startup_sequence_active"] is True
    assert diagnostics["startup_sequence_stage"] == "return_to_target"
    assert diagnostics["startup_sequence_completion_reason"] is None
    assert algo.requested_on_percent == pytest.approx(0.0)
    assert algo._state.committed_on_percent == pytest.approx(0.0)

    algo.calculate(
        target_temp=20.0,
        current_temp=20.0,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )

    diagnostics = algo.get_diagnostics()
    assert diagnostics["startup_sequence_active"] is False
    assert diagnostics["startup_sequence_stage"] == "completed"
    assert diagnostics["startup_sequence_completion_reason"] == "deadtime_on_off_identified"


def test_startup_bootstrap_exits_at_target_if_both_deadtimes_arrive_after_reheat_cycle_closed() -> None:
    """Delayed ON/OFF deadtime locks should complete once the target is reached."""
    algo = AdaptiveTPIAlgorithm(name="test-startup-bootstrap-late-deadtime")

    algo.calculate(
        target_temp=20.0,
        current_temp=20.0,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )
    algo.calculate(
        target_temp=20.0,
        current_temp=19.5,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )
    algo.calculate(
        target_temp=20.0,
        current_temp=20.5,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )

    diagnostics = algo.get_diagnostics()
    assert diagnostics["startup_sequence_stage"] == "return_to_target"
    assert diagnostics["startup_sequence_attempt"] == 1

    algo._state.deadtime_identification_count = 2
    algo._state.deadtime_on_locked = True
    algo._state.deadtime_off_locked = True
    algo.calculate(
        target_temp=20.0,
        current_temp=20.0,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )

    diagnostics = algo.get_diagnostics()
    assert diagnostics["startup_sequence_active"] is False
    assert diagnostics["startup_sequence_stage"] == "completed"
    assert diagnostics["startup_sequence_completion_reason"] == "deadtime_on_off_identified"
    assert algo.requested_on_percent == pytest.approx(0.0)
    assert algo._state.committed_on_percent == pytest.approx(0.0)


def test_startup_bootstrap_keeps_retrying_without_both_deadtimes() -> None:
    """Startup bootstrap should keep cycling instead of silently degrading."""
    algo = AdaptiveTPIAlgorithm(name="test-startup-bootstrap-abandon")

    algo.calculate(
        target_temp=20.0,
        current_temp=20.0,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )
    algo.calculate(
        target_temp=20.0,
        current_temp=19.5,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )
    algo.calculate(
        target_temp=20.0,
        current_temp=20.5,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )
    algo.calculate(
        target_temp=20.0,
        current_temp=20.0,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )
    algo.calculate(
        target_temp=20.0,
        current_temp=19.5,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )

    diagnostics = algo.get_diagnostics()
    assert diagnostics["startup_sequence_active"] is True
    assert diagnostics["startup_sequence_stage"] == "reactivation_to_upper_target"
    assert diagnostics["startup_sequence_completion_reason"] == "deadtime_on_off_retry"
    assert algo.requested_on_percent == pytest.approx(1.0)
    assert algo._state.committed_on_percent == pytest.approx(0.0)


def test_startup_bootstrap_forces_cycle_restart_once_when_cooldown_reaches_lower_target() -> None:
    """Bootstrap cooldown should request a single forced cycle restart at the lower threshold."""
    algo = AdaptiveTPIAlgorithm(name="test-startup-bootstrap-force-cooldown")

    algo.calculate(
        target_temp=20.0,
        current_temp=20.0,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )

    assert (
        algo.should_force_bootstrap_cycle_restart(
            target_temp=20.0,
            current_temp=19.8,
            hvac_mode="heat",
        )
        is False
    )
    assert (
        algo.should_force_bootstrap_cycle_restart(
            target_temp=20.0,
            current_temp=19.5,
            hvac_mode="heat",
        )
        is True
    )
    assert (
        algo.should_force_bootstrap_cycle_restart(
            target_temp=20.0,
            current_temp=19.4,
            hvac_mode="heat",
        )
        is False
    )


def test_startup_bootstrap_forces_cycle_restart_once_when_reheat_reaches_target() -> None:
    """Bootstrap reheat should request a single forced cycle restart at setpoint."""
    algo = AdaptiveTPIAlgorithm(name="test-startup-bootstrap-force-reheat")

    algo.calculate(
        target_temp=20.0,
        current_temp=20.0,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )
    algo.calculate(
        target_temp=20.0,
        current_temp=19.5,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )

    assert (
        algo.should_force_bootstrap_cycle_restart(
            target_temp=20.0,
            current_temp=19.9,
            hvac_mode="heat",
        )
        is False
    )
    assert (
        algo.should_force_bootstrap_cycle_restart(
            target_temp=20.0,
            current_temp=20.2,
            hvac_mode="heat",
        )
        is False
    )
    assert (
        algo.should_force_bootstrap_cycle_restart(
            target_temp=20.0,
            current_temp=20.5,
            hvac_mode="heat",
        )
        is True
    )


def test_startup_bootstrap_detects_mid_cycle_command_flip_after_calculation() -> None:
    """Bootstrap command flips should request an immediate restart on sensor refresh."""
    algo = AdaptiveTPIAlgorithm(name="test-startup-bootstrap-command-flip")

    algo.calculate(
        target_temp=20.0,
        current_temp=20.0,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )
    algo.calculate(
        target_temp=20.0,
        current_temp=19.5,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )
    previous_requested_on_percent = algo.requested_on_percent
    previous_bootstrap_command_on_percent = algo.startup_bootstrap_command_on_percent

    algo.calculate(
        target_temp=20.0,
        current_temp=20.5,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )

    assert previous_requested_on_percent == pytest.approx(1.0)
    assert previous_bootstrap_command_on_percent == pytest.approx(1.0)
    assert algo.requested_on_percent == pytest.approx(0.0)
    assert (
        algo.should_force_bootstrap_cycle_restart_after_calculation(
            previous_requested_on_percent=previous_requested_on_percent,
            previous_bootstrap_command_on_percent=previous_bootstrap_command_on_percent,
        )
        is True
    )


def test_startup_bootstrap_detects_mid_cycle_command_flip_after_thermostat_recalculate() -> None:
    """Bootstrap command flips should still restart when thermostat recalculate ran first."""
    algo = AdaptiveTPIAlgorithm(name="test-startup-bootstrap-command-flip-over-valve")

    algo.calculate(
        target_temp=20.0,
        current_temp=20.0,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )
    algo.calculate(
        target_temp=20.0,
        current_temp=19.5,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )
    algo.update_realized_power(1.0)

    algo.calculate(
        target_temp=20.0,
        current_temp=20.5,
        ext_current_temp=20.0,
        slope=None,
        hvac_mode="heat",
    )

    assert algo.requested_on_percent == pytest.approx(0.0)
    assert algo.startup_bootstrap_command_on_percent == pytest.approx(0.0)
    assert (
        algo.should_force_bootstrap_cycle_restart_after_calculation(
            previous_requested_on_percent=0.0,
            previous_bootstrap_command_on_percent=0.0,
        )
        is True
    )


@pytest.mark.asyncio
async def test_store_migration_keeps_v1_persisted_payload_readable() -> None:
    """Store migration should not discard compatible learning payloads."""
    store = object.__new__(_AdaptiveTPIStore)
    payload = {
        "schema_version": 1,
        "state": {"k_int": 0.7, "k_ext": 0.03},
    }

    migrated = await store._async_migrate_func(1, 1, payload)

    assert migrated is payload


@pytest.mark.asyncio
async def test_handler_loads_v1_persisted_payload_after_store_migration() -> None:
    """Schema-1 learning payloads should remain usable after storage migration."""
    payload = {
        "schema_version": 1,
        "state": {"k_int": 0.7, "k_ext": 0.03},
        "cycle_min": 5.0,
        "last_accepted_at": None,
        "saved_at": None,
    }

    class StoreStub:
        async def async_load(self):
            return payload

    thermostat = SimpleNamespace(
        prop_algorithm=SimpleNamespace(load_state=MagicMock()),
        cycle_min=5,
    )
    handler = object.__new__(AdaptiveTPIHandler)
    handler._thermostat = thermostat
    handler._store = StoreStub()
    handler._refresh_published_diagnostics = MagicMock()

    await handler._async_load_persisted_state()

    thermostat.prop_algorithm.load_state.assert_called_once_with(
        payload["state"],
        current_cycle_min=5.0,
        persisted_cycle_min=5.0,
        last_accepted_at=None,
        saved_at=None,
    )
    handler._refresh_published_diagnostics.assert_called_once_with()


@pytest.mark.asyncio
async def test_handler_ignores_unsupported_persisted_payload_schema() -> None:
    """Unsupported learning payload schemas should not be restored."""
    payload = {
        "schema_version": PERSISTENCE_SCHEMA_VERSION + 1,
        "state": {"k_int": 0.7, "k_ext": 0.03},
    }

    class StoreStub:
        async def async_load(self):
            return payload

    thermostat = SimpleNamespace(
        prop_algorithm=SimpleNamespace(load_state=MagicMock()),
        cycle_min=5,
    )
    handler = object.__new__(AdaptiveTPIHandler)
    handler._thermostat = thermostat
    handler._store = StoreStub()
    handler._refresh_published_diagnostics = MagicMock()

    await handler._async_load_persisted_state()

    thermostat.prop_algorithm.load_state.assert_not_called()
    handler._refresh_published_diagnostics.assert_not_called()


@pytest.mark.asyncio
async def test_handler_forces_bootstrap_cycle_restart_on_state_change() -> None:
    """State changes should trigger an immediate forced control pass when bootstrap hits a limit."""
    thermostat = SimpleNamespace(
        prop_algorithm=SimpleNamespace(
            should_force_bootstrap_cycle_restart=MagicMock(return_value=True)
        ),
        cycle_scheduler=SimpleNamespace(is_cycle_running=True),
        target_temperature=20.0,
        current_temperature=19.7,
        vtherm_hvac_mode="heat",
        recalculate=MagicMock(),
        async_control_heating=AsyncMock(),
    )
    handler = object.__new__(AdaptiveTPIHandler)
    handler._thermostat = thermostat

    await handler.on_state_changed(True)

    thermostat.prop_algorithm.should_force_bootstrap_cycle_restart.assert_called_once_with(
        target_temp=20.0,
        current_temp=19.7,
        hvac_mode="heat",
    )
    thermostat.recalculate.assert_called_once_with(force=True)
    thermostat.async_control_heating.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
async def test_handler_forces_immediate_restart_when_bootstrap_changes_command_mid_cycle() -> None:
    """Temperature refreshes should force the scheduler when bootstrap flips the command."""
    thermostat = SimpleNamespace(
        target_temperature=22.5,
        current_temperature=22.5,
        current_outdoor_temperature=15.8,
        last_temperature_slope=0.0,
        vtherm_hvac_mode="heat",
        is_overpowering_detected=False,
        hvac_off_reason=None,
        cycle_min=5,
        prop_algorithm=SimpleNamespace(
            requested_on_percent=1.0,
            startup_bootstrap_command_on_percent=1.0,
            calculate=MagicMock(),
            get_diagnostics=MagicMock(
                return_value={"startup_sequence_stage": "cooldown_below_target"}
            ),
            should_force_bootstrap_cycle_restart_after_calculation=MagicMock(
                return_value=True
            ),
        ),
        cycle_scheduler=SimpleNamespace(is_cycle_running=True, start_cycle=AsyncMock()),
    )
    handler = object.__new__(AdaptiveTPIHandler)
    handler._thermostat = thermostat
    handler._published_diagnostics = {}
    handler._should_publish_intermediate = False

    def _flip_bootstrap_command(*_args, **_kwargs) -> None:
        thermostat.prop_algorithm.requested_on_percent = 0.0
        thermostat.prop_algorithm.startup_bootstrap_command_on_percent = 0.0

    thermostat.prop_algorithm.calculate.side_effect = _flip_bootstrap_command

    await handler.control_heating(force=False)

    thermostat.prop_algorithm.should_force_bootstrap_cycle_restart_after_calculation.assert_called_once_with(
        previous_requested_on_percent=1.0,
        previous_bootstrap_command_on_percent=1.0,
    )
    thermostat.cycle_scheduler.start_cycle.assert_awaited_once_with("heat", 0.0, True)
    assert handler._should_publish_intermediate is True


@pytest.mark.asyncio
async def test_handler_allows_intermediate_publication_during_normal_control_passes() -> None:
    """Normal control passes should still allow VT to publish updated temperatures."""
    thermostat = SimpleNamespace(
        target_temperature=22.5,
        current_temperature=22.4,
        current_outdoor_temperature=15.8,
        last_temperature_slope=0.0,
        vtherm_hvac_mode="heat",
        is_overpowering_detected=False,
        hvac_off_reason=None,
        cycle_min=5,
        prop_algorithm=SimpleNamespace(
            requested_on_percent=0.4,
            startup_bootstrap_command_on_percent=None,
            calculate=MagicMock(),
        ),
        cycle_scheduler=SimpleNamespace(is_cycle_running=False, start_cycle=AsyncMock()),
    )
    handler = object.__new__(AdaptiveTPIHandler)
    handler._thermostat = thermostat
    handler._published_diagnostics = {}
    handler._should_publish_intermediate = False

    await handler.control_heating(force=False)

    thermostat.cycle_scheduler.start_cycle.assert_awaited_once_with("heat", 0.4, False)
    assert handler.should_publish_intermediate() is True


@pytest.mark.asyncio
async def test_handler_refreshes_diagnostics_after_forced_control_pass() -> None:
    """Forced control passes should publish refreshed diagnostics immediately."""
    thermostat = SimpleNamespace(
        target_temperature=22.0,
        current_temperature=22.4,
        current_outdoor_temperature=15.8,
        last_temperature_slope=3.5,
        vtherm_hvac_mode="heat",
        is_overpowering_detected=False,
        hvac_off_reason=None,
        cycle_min=4,
        prop_algorithm=SimpleNamespace(
            calculate=MagicMock(),
            get_diagnostics=MagicMock(return_value={"startup_sequence_stage": "completed"}),
            requested_on_percent=0.0,
        ),
        cycle_scheduler=SimpleNamespace(start_cycle=AsyncMock()),
    )
    handler = object.__new__(AdaptiveTPIHandler)
    handler._thermostat = thermostat
    handler._published_diagnostics = {"startup_sequence_stage": "passive_drift_phase"}
    handler._should_publish_intermediate = False

    await handler.control_heating(force=True)

    thermostat.prop_algorithm.calculate.assert_called_once()
    thermostat.cycle_scheduler.start_cycle.assert_awaited_once_with("heat", 0.0, True)
    assert handler._published_diagnostics == {"startup_sequence_stage": "completed"}


@pytest.mark.asyncio
async def test_service_reset_learning_recalculates_before_forced_control() -> None:
    """Learning reset should realign thermostat-side state before forcing cycle control."""
    thermostat = SimpleNamespace(
        prop_algorithm=SimpleNamespace(reset_learning=MagicMock()),
        recalculate=MagicMock(),
        async_control_heating=AsyncMock(),
        async_write_ha_state=MagicMock(),
    )
    handler = object.__new__(AdaptiveTPIHandler)
    handler._thermostat = thermostat
    handler._async_delete_persisted_state = AsyncMock()
    handler._refresh_published_diagnostics = MagicMock()
    handler.update_attributes = MagicMock()

    await handler.service_reset_learning()

    thermostat.prop_algorithm.reset_learning.assert_called_once_with()
    handler._async_delete_persisted_state.assert_awaited_once_with()
    handler._refresh_published_diagnostics.assert_called_once_with()
    thermostat.recalculate.assert_called_once_with(force=True)
    thermostat.async_control_heating.assert_awaited_once_with(force=True)
    handler.update_attributes.assert_called_once_with()
    thermostat.async_write_ha_state.assert_called_once_with()


@pytest.mark.asyncio
async def test_delete_persisted_state_uses_store_api_when_available() -> None:
    """Learning reset should clear Home Assistant storage through the Store API."""
    store = SimpleNamespace(async_remove=AsyncMock())
    handler = object.__new__(AdaptiveTPIHandler)
    handler._store = store

    await handler._async_delete_persisted_state()

    store.async_remove.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_service_reset_valve_curve_recalculates_before_forced_control() -> None:
    """Valve curve reset should realign thermostat-side state before forcing cycle control."""
    thermostat = SimpleNamespace(
        prop_algorithm=SimpleNamespace(reset_valve_curve=MagicMock()),
        recalculate=MagicMock(),
        async_control_heating=AsyncMock(),
        async_write_ha_state=MagicMock(),
    )
    handler = object.__new__(AdaptiveTPIHandler)
    handler._thermostat = thermostat
    handler._async_save_persisted_state = AsyncMock()
    handler._refresh_published_diagnostics = MagicMock()
    handler.update_attributes = MagicMock()

    await handler.service_reset_valve_curve()

    thermostat.prop_algorithm.reset_valve_curve.assert_called_once_with()
    handler._async_save_persisted_state.assert_awaited_once_with()
    handler._refresh_published_diagnostics.assert_called_once_with()
    thermostat.recalculate.assert_called_once_with(force=True)
    thermostat.async_control_heating.assert_awaited_once_with(force=True)
    handler.update_attributes.assert_called_once_with()
    thermostat.async_write_ha_state.assert_called_once_with()


def test_diagnostics_expose_normalized_units_when_cycle_duration_is_known() -> None:
    """Diagnostics should expose user-facing normalized units in addition to per-cycle values."""
    algo = AdaptiveTPIAlgorithm(name="test-diag-units")
    algo._state.nd_hat = 2.0
    algo._state.a_hat = 0.2
    algo._state.b_hat = 0.03
    algo._state.cycle_min_at_last_accepted_cycle = 5.0

    diagnostics = algo.get_diagnostics()

    assert diagnostics["deadtime_cycles"] == pytest.approx(2.0)
    assert diagnostics["deadtime_minutes"] == pytest.approx(10.0)
    assert diagnostics["control_rate_per_hour"] == pytest.approx(2.4)
    assert diagnostics["drift_rate_per_hour"] == pytest.approx(0.36)
    assert diagnostics["thermal_time_constant_hours"] == pytest.approx(1.0 / 0.36)


def test_diagnostics_publish_measured_deadtime_minutes_when_available() -> None:
    """Measured deadtime minutes should take priority over a normalized conversion."""
    algo = AdaptiveTPIAlgorithm(name="test-diag-deadtime-minutes", debug_mode=True)
    algo._state.nd_hat = 2.0
    algo._state.deadtime_minutes = 11.5
    algo._state.cycle_min_at_last_accepted_cycle = 5.0

    diagnostics = algo.get_diagnostics()

    assert diagnostics["deadtime_minutes"] == pytest.approx(11.5)
    assert diagnostics["debug"]["deadtime_min"] == pytest.approx(11.5)


def test_diagnostics_expose_valve_curve_learning_state() -> None:
    """Diagnostics should surface valve curve learning counters and status."""
    algo = AdaptiveTPIAlgorithm(
        name="test-valve-curve-diagnostics",
        actuator_mode=ACTUATOR_MODE_VALVE,
        debug_mode=True,
    )
    algo._valve_curve.observe(
        u_valve=0.20,
        dTdt=0.10,
        delta_out=5.0,
        a_hat=0.4,
        b_hat=0.02,
        b_converged=True,
        mode_sign=1,
    )
    algo._refresh_valve_curve_state()

    diagnostics = algo.get_diagnostics()
    assert diagnostics["actuator_mode"] == ACTUATOR_MODE_VALVE
    assert diagnostics["valve_curve_learning_enabled"] is True
    assert diagnostics["valve_curve_observations_accepted"] == 1
    assert diagnostics["valve_curve_observations_rejected"] == 0
    assert diagnostics["valve_curve_last_reason"] == "sample_accepted"
    assert diagnostics["debug"]["valve_curve_learning_enabled"] is True


def test_invalid_temperature_data_rejects_cycle_and_disables_output() -> None:
    """Missing temperatures should reject the cycle and expose the freeze reason."""
    algo = AdaptiveTPIAlgorithm(name="test-invalid", debug_mode=True)

    algo.calculate(
        target_temp=None,
        current_temp=20.0,
        ext_current_temp=8.0,
        slope=None,
        hvac_mode="heat",
    )

    diagnostics = algo.get_diagnostics()
    assert algo._state.committed_on_percent is None
    assert algo.requested_on_percent is None
    assert diagnostics["last_runtime_blocker"] == "missing_temperature"
    assert diagnostics["debug"]["last_cycle_classification"] == "rejected"


def test_off_mode_forces_zero_output() -> None:
    """Off mode should clamp the command to zero even with valid temperatures."""
    algo = AdaptiveTPIAlgorithm(name="test-off", debug_mode=True)

    algo.calculate(
        target_temp=21.0,
        current_temp=20.0,
        ext_current_temp=8.0,
        slope=None,
        hvac_mode="off",
    )

    diagnostics = algo.get_diagnostics()
    assert algo.requested_on_percent == pytest.approx(0.0)
    assert algo._state.committed_on_percent == pytest.approx(0.0)
    assert diagnostics["last_runtime_blocker"] == "hvac_mode_incompatible"
    assert diagnostics["debug"]["last_cycle_classification"] == "rejected"


def test_step_detection_finds_clean_on_transition() -> None:
    """A clean OFF→ON transition after stable OFF cycles should be detected."""
    observations = _make_step_observations(nd=2, n_off=4, n_on=1)
    step_index = _find_latest_step(observations, last_processed_step_index=-1)
    assert step_index == 4


def test_step_detection_ignores_no_off_period() -> None:
    """A step with no preceding OFF cycle (previous power too high) must not be detected."""
    observations = _make_step_observations(nd=2, n_off=0, n_on=10)
    step_index = _find_latest_step(observations, last_processed_step_index=-1)
    assert step_index is None


def test_step_detection_uses_applied_demand() -> None:
    """Deadtime step detection must use the model-space command."""
    observations = (
        CycleHistoryEntry(
            tin=19.0,
            tout=10.0,
            target_temp=21.0,
            applied_power=0.0,
            applied_demand=0.0,
            is_valid=True,
            is_informative=True,
            is_estimator_informative=True,
        ),
        CycleHistoryEntry(
            tin=19.0,
            tout=10.0,
            target_temp=21.0,
            applied_power=0.12,
            applied_demand=0.65,
            is_valid=True,
            is_informative=True,
            is_estimator_informative=True,
        ),
    )
    assert _find_latest_step(observations, last_processed_step_index=-1) == 1


def test_rise_delay_detects_rise() -> None:
    """_measure_rise_delay must return a StepIdentification when a rise is detected."""
    observations = _make_step_observations(nd=2, n_off=4)
    result = _measure_rise_delay(observations, step_index=4)
    assert isinstance(result, StepIdentification)
    assert result.nd_cycles >= 1.0


def test_rise_delay_aborts_on_power_cut() -> None:
    """A power drop below STEP_ABORT_POWER_NEW during collection must abort."""
    base = list(_make_step_observations(nd=2, n_off=4, n_on=5))
    entry = base[7]
    base[7] = CycleHistoryEntry(
        tin=entry.tin,
        tout=entry.tout,
        target_temp=entry.target_temp,
        applied_power=0.0,
        is_valid=True,
        is_informative=True,
        is_estimator_informative=True,
        cycle_duration_min=5.0,
    )
    result = _measure_rise_delay(tuple(base), step_index=4)
    assert result == "aborted"


def test_rise_delay_identification_has_valid_quality() -> None:
    """A valid step response must yield a positive finite quality from rise-delay."""
    observations = _make_step_observations(nd=2, n_off=4)
    result = _measure_rise_delay(observations, step_index=4)
    assert isinstance(result, StepIdentification)
    assert math.isfinite(result.nd_cycles)
    assert result.nd_cycles >= 0.0
    assert 0.0 < result.quality <= 1.0


def test_rise_delay_single_off_cycle_accepted() -> None:
    """A step preceded by a single OFF cycle must be detected and identified."""
    observations = _make_step_observations(nd=1, n_off=1, n_on=12)
    step_index = _find_latest_step(observations, last_processed_step_index=-1)
    assert step_index == 1
    result = _measure_rise_delay(observations, step_index=1)
    assert isinstance(result, StepIdentification)


def test_rise_delay_slow_responder_before_ceiling() -> None:
    """A slow responder detected before the ceiling must have reduced quality."""
    tin = 10.0
    tout = 5.0
    target = 20.0
    entries: list[CycleHistoryEntry] = []
    # 2 OFF cycles
    for _ in range(2):
        entries.append(
            CycleHistoryEntry(
                tin=tin,
                tout=tout,
                target_temp=target,
                applied_power=0.0,
                is_valid=True,
                is_informative=True,
                is_estimator_informative=True,
            )
        )
    # 6 dead cycles (ON power, tin barely moves)
    for k in range(6):
        entries.append(
            CycleHistoryEntry(
                tin=tin + 0.01 * k,
                tout=tout,
                target_temp=target,
                applied_power=0.8,
                is_valid=True,
                is_informative=True,
                is_estimator_informative=True,
            )
        )
    # Rise at cycle 7 (+0.15 °C from step)
    entries.append(
        CycleHistoryEntry(
            tin=tin + 0.15,
            tout=tout,
            target_temp=target,
            applied_power=0.8,
            is_valid=True,
            is_informative=True,
            is_estimator_informative=True,
        )
    )
    observations = tuple(entries)
    step_index = _find_latest_step(observations, last_processed_step_index=-1)
    assert step_index == 2
    result = _measure_rise_delay(observations, step_index=2)
    assert isinstance(result, StepIdentification)
    assert result.nd_cycles == pytest.approx(6.0)
    assert 0.0 < result.quality <= 1.0


def test_rise_delay_ceiling_hit_returns_capped_nd_with_reduced_quality() -> None:
    """When no rise is detected within N_MAX_RISE_CYCLES, nd is capped and quality halved."""
    tin = 10.0
    tout = 5.0
    target = 20.0
    entries: list[CycleHistoryEntry] = []
    # 2 OFF cycles
    for _ in range(2):
        entries.append(
            CycleHistoryEntry(
                tin=tin,
                tout=tout,
                target_temp=target,
                applied_power=0.0,
                is_valid=True,
                is_informative=True,
                is_estimator_informative=True,
            )
        )
    # N_MAX_RISE_CYCLES + 1 ON cycles with flat tin (no rise)
    for _ in range(N_MAX_RISE_CYCLES + 1):
        entries.append(
            CycleHistoryEntry(
                tin=tin,
                tout=tout,
                target_temp=target,
                applied_power=0.8,
                is_valid=True,
                is_informative=True,
                is_estimator_informative=True,
            )
        )
    observations = tuple(entries)
    result = _measure_rise_delay(observations, step_index=2)
    assert isinstance(result, StepIdentification)
    assert result.nd_cycles == pytest.approx(float(N_MAX_RISE_CYCLES))
    # Quality must be reduced (halved) compared to a clean rise
    assert result.quality < 0.5


def test_rise_delay_aborts_on_setpoint_jump() -> None:
    """A setpoint jump during collection must abort the identification."""
    observations = _make_step_observations(nd=2, n_off=4, n_on=10)
    base = list(observations)
    # Inject a setpoint jump at index 7
    entry = base[7]
    base[7] = CycleHistoryEntry(
        tin=entry.tin,
        tout=entry.tout,
        target_temp=entry.target_temp + 2.0,
        applied_power=entry.applied_power,
        is_valid=True,
        is_informative=True,
        is_estimator_informative=True,
        cycle_duration_min=5.0,
    )
    result = _measure_rise_delay(tuple(base), step_index=4)
    assert result == "aborted"


def test_b_proxy_positive_from_off_period() -> None:
    """The b proxy from the OFF period must be a positive finite value."""
    # Simulate a cooling room: tin starts at 10°C, tout=0°C, b=0.25.
    # |tin - tout| >> MIN_B_DELTA_OUT so b_proxy has signal to work with.
    b_true = 0.25
    tout = 0.0
    tin = 10.0
    entries: list[CycleHistoryEntry] = []
    for _ in range(6):
        entries.append(
            CycleHistoryEntry(
                tin=tin,
                tout=tout,
                target_temp=20.0,
                applied_power=0.0,
                is_valid=True,
                is_informative=True,
                is_estimator_informative=True,
            )
        )
        tin = tin - b_true * (tin - tout)
    b = _compute_b_proxy(tuple(entries), step_index=5)
    assert b is not None
    assert b == pytest.approx(b_true, rel=1e-3)
    assert math.isfinite(b)


def test_deadtime_model_locks_after_consistent_identifications() -> None:
    """The model must lock once enough consistent identifications are stored."""
    model = DeadtimeModel()
    for i in range(3):
        model._identifications.append(
            StepIdentification(
                nd_cycles=2.0 + 0.05 * i,
                nd_minutes=10.0 + 0.25 * i,
                quality=0.85,
                b_proxy=0.03,
                cycle_index=i * 30,
            )
        )
    result = model._recompute_nd_hat()
    assert result.locked is True
    assert result.lock_reason is None
    assert result.nd_hat == pytest.approx(2.05, abs=0.1)
    assert result.nd_minutes == pytest.approx(10.25, abs=0.1)
    assert result.c_nd >= CONFIDENCE_LOCK_THRESHOLD
    assert result.best_candidate_b == pytest.approx(0.03)


def test_deadtime_model_keeps_minutes_from_selected_deadtime() -> None:
    """The published minute value must stay attached to the selected deadtime."""
    model = DeadtimeModel()
    model._identifications.extend(
        (
            StepIdentification(
                nd_cycles=1.0,
                nd_minutes=9.0,
                quality=0.3,
                b_proxy=0.02,
                cycle_index=0,
            ),
            StepIdentification(
                nd_cycles=2.0,
                nd_minutes=5.0,
                quality=0.3,
                b_proxy=0.03,
                cycle_index=1,
            ),
            StepIdentification(
                nd_cycles=3.0,
                nd_minutes=6.0,
                quality=0.4,
                b_proxy=0.04,
                cycle_index=2,
            ),
        )
    )

    result = model._recompute_nd_hat()

    assert result.nd_hat == pytest.approx(2.0)
    assert result.nd_minutes == pytest.approx(5.0)


def test_deadtime_model_locks_on_single_clean_identification() -> None:
    """A single high-quality identification must be enough to lock."""
    model = DeadtimeModel()
    model._identifications.append(
        StepIdentification(nd_cycles=2.0, quality=0.9, b_proxy=None, cycle_index=0)
    )
    result = model._recompute_nd_hat()
    assert result.locked is True
    assert result.lock_reason is None
    assert result.c_nd >= CONFIDENCE_LOCK_THRESHOLD


def test_deadtime_model_keeps_on_lock_after_incoherent_identification() -> None:
    """A locked ON deadtime must remain usable after a later inconsistent sample."""
    model = DeadtimeModel()
    model.record_identification(
        StepIdentification(
            nd_cycles=2.0,
            nd_minutes=10.0,
            quality=0.9,
            b_proxy=None,
            cycle_index=0,
            transition="on",
        )
    )

    result = model.record_identification(
        StepIdentification(
            nd_cycles=8.0,
            nd_minutes=40.0,
            quality=0.9,
            b_proxy=None,
            cycle_index=1,
            transition="on",
        )
    )

    assert result.locked is True
    assert result.deadtime_on_locked is True
    assert result.nd_hat == pytest.approx(2.0)
    assert result.nd_minutes == pytest.approx(10.0)


def test_deadtime_model_no_lock_when_single_identification_quality_too_low() -> None:
    """A single identification with quality below threshold must not lock."""
    model = DeadtimeModel()
    model._identifications.append(
        StepIdentification(nd_cycles=2.0, quality=0.3, b_proxy=None, cycle_index=0)
    )
    result = model._recompute_nd_hat()
    assert result.locked is False
    assert result.lock_reason == "deadtime_confidence_low"
    assert result.c_nd < CONFIDENCE_LOCK_THRESHOLD


def test_deadtime_model_no_lock_when_spread_too_high() -> None:
    """High relative spread across identifications must prevent locking."""
    model = DeadtimeModel()
    for nd in [1.0, 5.0, 9.0]:
        model._identifications.append(
            StepIdentification(nd_cycles=nd, quality=0.9, b_proxy=None, cycle_index=0)
        )
    result = model._recompute_nd_hat()
    assert result.locked is False
    assert result.lock_reason in (
        "deadtime_insufficient_separation",
        "deadtime_confidence_low",
    )


def test_deadtime_model_nd_hat_can_be_non_integer() -> None:
    """nd_hat must be a continuous float, not restricted to integer values."""
    model = DeadtimeModel()
    for nd in [1.3, 1.5, 1.7]:
        model._identifications.append(
            StepIdentification(nd_cycles=nd, quality=0.9, b_proxy=None, cycle_index=0)
        )
    result = model._recompute_nd_hat()
    assert not float(result.nd_hat).is_integer() or result.nd_hat == pytest.approx(1.5)


def test_deadtime_persistence_roundtrip() -> None:
    """Identifications must survive a serialise / deserialise cycle unchanged."""
    model = DeadtimeModel()
    model._identifications.append(
        StepIdentification(
            nd_cycles=2.3,
            nd_minutes=11.7,
            quality=0.75,
            b_proxy=0.04,
            cycle_index=10,
        )
    )
    model._last_processed_step_index = 10
    model._recompute_nd_hat()

    saved = model.to_persisted_dict(cycle_min=11.7 / 2.3)

    model2 = DeadtimeModel()
    model2.load_persisted_dict(saved, cycle_min=11.7 / 2.3)

    assert len(model2._identifications) == 1
    assert model2._identifications[0].nd_cycles == pytest.approx(2.3)
    assert model2._identifications[0].nd_minutes == pytest.approx(11.7)
    assert model2._identifications[0].quality == pytest.approx(0.75)
    assert model2._identifications[0].b_proxy == pytest.approx(0.04)
    assert model2._last_processed_step_index == 10
    assert model2.nd_hat == pytest.approx(2.3)
    assert model2.nd_minutes == pytest.approx(11.7)


def test_deadtime_persistence_roundtrip_keeps_on_off_families() -> None:
    """ON and OFF deadtime identifications must persist as separate families."""
    model = DeadtimeModel()
    model.record_identification(
        StepIdentification(
            nd_cycles=2.0,
            nd_minutes=10.0,
            quality=0.9,
            b_proxy=0.04,
            cycle_index=10,
            transition="on",
        )
    )
    model.record_identification(
        StepIdentification(
            nd_cycles=3.0,
            nd_minutes=15.0,
            quality=0.8,
            b_proxy=None,
            cycle_index=11,
            transition="off",
        )
    )

    saved = model.to_persisted_dict(cycle_min=5.0)
    model2 = DeadtimeModel()
    assert model2.load_persisted_dict(saved, cycle_min=5.0) is True
    result = model2.last_result

    assert len(model2._identifications) == 1
    assert len(model2._identifications_off) == 1
    assert result.deadtime_on_locked is True
    assert result.deadtime_off_locked is True
    assert result.nd_hat_on == pytest.approx(2.0)
    assert result.nd_minutes_on == pytest.approx(10.0)
    assert result.nd_hat_off == pytest.approx(3.0)
    assert result.nd_minutes_off == pytest.approx(15.0)


def test_deadtime_persistence_keeps_locked_snapshot_after_incoherent_sample() -> None:
    """A persisted locked snapshot must survive incoherent later samples."""
    model = DeadtimeModel()
    model.record_identification(
        StepIdentification(
            nd_cycles=2.0,
            nd_minutes=10.0,
            quality=0.9,
            b_proxy=0.04,
            cycle_index=1,
            transition="on",
        )
    )
    model.record_identification(
        StepIdentification(
            nd_cycles=8.0,
            nd_minutes=40.0,
            quality=0.9,
            b_proxy=None,
            cycle_index=2,
            transition="on",
        )
    )
    assert model.last_result.locked is True
    assert model.last_result.nd_hat == pytest.approx(2.0)

    saved = model.to_persisted_dict(cycle_min=5.0)
    restored = DeadtimeModel()
    assert restored.load_persisted_dict(saved, cycle_min=5.0) is True

    assert restored.last_result.locked is True
    assert restored.last_result.deadtime_on_locked is True
    assert restored.last_result.nd_hat == pytest.approx(2.0)
    assert restored.last_result.nd_minutes == pytest.approx(10.0)


def test_deadtime_persistence_without_transition_restores_on_only() -> None:
    """Legacy identifications without transition belong to the ON family only."""
    model = DeadtimeModel()
    model.load_persisted_dict(
        {
            "persistence_units": "time_canonical",
            "identifications": [
                {
                    "nd_minutes": 10.0,
                    "quality": 0.9,
                    "cycle_index": 1,
                }
            ],
        },
        cycle_min=5.0,
    )

    assert len(model._identifications) == 1
    assert len(model._identifications_off) == 0
    assert model.last_result.deadtime_on_locked is True
    assert model.last_result.deadtime_off_locked is False


def test_deadtime_old_format_silently_ignored() -> None:
    """A persisted dict with the old cycle_history key must be silently discarded."""
    old_format = {
        "cycle_history": [
            {"tin": 19.0, "tout": 10.0, "target_temp": 21.0, "applied_power": 0.7}
        ],
        "best_candidate_history": [1, 1, 1],
    }
    model = DeadtimeModel()
    model.load_persisted_dict(old_format)
    assert len(model._identifications) == 0
    assert model.nd_hat == pytest.approx(0.0)
    assert model.locked is False


def test_startup_bootstrap_waits_for_on_and_off_deadtime_locks() -> None:
    """Startup bootstrap should complete only after both transition families lock."""
    bootstrap = StartupBootstrapController()

    first = bootstrap.evaluate(
        target_temp=20.0,
        current_temp=20.0,
        deadtime_identification_count=0,
        deadtime_on_locked=False,
        deadtime_off_locked=False,
        heating_enabled=True,
    )
    assert first.active is True
    assert first.command_on_percent == pytest.approx(0.0)

    reheat = bootstrap.evaluate(
        target_temp=20.0,
        current_temp=19.5,
        deadtime_identification_count=1,
        deadtime_on_locked=True,
        deadtime_off_locked=False,
        heating_enabled=True,
    )
    assert reheat.active is True
    assert reheat.command_on_percent == pytest.approx(1.0)

    final_cooldown = bootstrap.evaluate(
        target_temp=20.0,
        current_temp=20.5,
        deadtime_identification_count=1,
        deadtime_on_locked=True,
        deadtime_off_locked=False,
        heating_enabled=True,
    )
    assert final_cooldown.active is True
    assert final_cooldown.command_on_percent == pytest.approx(0.0)

    retry = bootstrap.evaluate(
        target_temp=20.0,
        current_temp=20.0,
        deadtime_identification_count=1,
        deadtime_on_locked=True,
        deadtime_off_locked=False,
        heating_enabled=True,
    )
    assert retry.active is True
    assert retry.completion_reason == "deadtime_on_off_retry"

    completed = bootstrap.evaluate(
        target_temp=20.0,
        current_temp=20.3,
        deadtime_identification_count=2,
        deadtime_on_locked=True,
        deadtime_off_locked=True,
        heating_enabled=True,
    )
    assert completed.active is True
    assert completed.stage == STARTUP_BOOTSTRAP_FINAL_COOLDOWN
    assert completed.command_on_percent == pytest.approx(0.0)

    completed = bootstrap.evaluate(
        target_temp=20.0,
        current_temp=20.0,
        deadtime_identification_count=2,
        deadtime_on_locked=True,
        deadtime_off_locked=True,
        heating_enabled=True,
    )
    assert completed.active is False
    assert completed.completion_reason == "deadtime_on_off_identified"


def test_estimator_updates_stay_bounded_under_extreme_samples() -> None:
    """The decoupled estimators should never leave their configured bounds."""
    estimator = ParameterEstimator()

    for _ in range(50):
        estimator.update_b(
            BSample(
                dTdt=-10.0,
                delta_out=8.0,
                setpoint_error=1.5,
                u_eff=0.0,
            )
        )
        update = estimator.update_a(
            ASample(
                dTdt=10.0,
                delta_out=8.0,
                setpoint_error=1.5,
                u_eff=1.0,
            )
        )

    for _ in range(50):
        estimator.update_b(
            BSample(
                dTdt=10.0,
                delta_out=8.0,
                setpoint_error=1.5,
                u_eff=0.0,
            )
        )
        update = estimator.update_a(
            ASample(
                dTdt=-10.0,
                delta_out=8.0,
                setpoint_error=1.5,
                u_eff=1.0,
            )
        )
        assert A_MIN <= update.a_hat <= A_MAX
        assert B_MIN <= update.b_hat <= B_MAX
        assert 0.0 <= update.c_a <= 1.0
        assert 0.0 <= update.c_b <= 1.0


def test_estimator_can_seed_b_from_deadtime_proxy() -> None:
    """A deadtime-side `b` proxy may bootstrap the explicit estimator once."""
    estimator = ParameterEstimator()

    update = estimator.seed_b_from_deadtime_proxy(0.03)

    assert update.updated is True
    assert update.b_updated is True
    assert update.b_hat == pytest.approx(0.03)
    assert update.b_samples_count == 1
    assert update.b_last_reason == "b_seeded_from_deadtime"
    assert update.i_b == pytest.approx(0.0)


def test_learning_window_uses_applied_demand_for_regime_and_u_eff() -> None:
    """Learning windows must consume model-space demand, not actuator position."""
    observations = (
        CycleHistoryEntry(
            tin=19.0,
            tout=10.0,
            target_temp=21.0,
            applied_power=0.12,
            applied_demand=0.50,
            is_valid=True,
            is_informative=True,
            is_estimator_informative=True,
            cycle_duration_min=10.0,
        ),
        CycleHistoryEntry(
            tin=19.2,
            tout=10.0,
            target_temp=21.0,
            applied_power=0.12,
            applied_demand=0.50,
            is_valid=True,
            is_informative=False,
            is_estimator_informative=False,
            cycle_duration_min=10.0,
        ),
    )
    result = build_anchored_learning_window(
        observations,
        nd_hat=0.0,
        regime=WINDOW_REGIME_ON,
        end_index=0,
    )
    assert result.sample is not None
    assert result.sample.u_eff == pytest.approx(0.50)


def test_learning_window_rejects_on_window_when_applied_demand_is_low() -> None:
    """A low model-space demand remains OFF even if the raw actuator value differs."""
    observations = (
        CycleHistoryEntry(
            tin=19.0,
            tout=10.0,
            target_temp=21.0,
            applied_power=0.50,
            applied_demand=0.05,
            is_valid=True,
            is_informative=True,
            is_estimator_informative=True,
            cycle_duration_min=10.0,
        ),
        CycleHistoryEntry(
            tin=19.2,
            tout=10.0,
            target_temp=21.0,
            applied_power=0.50,
            applied_demand=0.05,
            is_valid=True,
            is_informative=False,
            is_estimator_informative=False,
            cycle_duration_min=10.0,
        ),
    )
    result = build_anchored_learning_window(
        observations,
        nd_hat=0.0,
        regime=WINDOW_REGIME_ON,
        end_index=0,
    )
    assert result.sample is None
    assert result.reason == "on_window_anchor_regime_mismatch"


def test_learning_window_can_extend_across_multiple_off_cycles() -> None:
    """A weak OFF cycle may be extended with the next one to produce a usable slope."""
    result = build_learning_window(
        (
            CycleHistoryEntry(
                tin=19.0,
                tout=10.0,
                target_temp=21.0,
                applied_power=0.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=True,
                cycle_duration_min=5.0,
            ),
            CycleHistoryEntry(
                tin=19.1,
                tout=10.0,
                target_temp=21.0,
                applied_power=0.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=True,
                cycle_duration_min=5.0,
            ),
            CycleHistoryEntry(
                tin=18.9,
                tout=10.0,
                target_temp=21.0,
                applied_power=0.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=True,
                cycle_duration_min=5.0,
            ),
            CycleHistoryEntry(
                tin=18.75,
                tout=10.0,
                target_temp=21.0,
                applied_power=0.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=True,
                cycle_duration_min=5.0,
            ),
        ),
        nd_hat=1.0,
        regime=WINDOW_REGIME_OFF,
    )

    assert result.sample is not None
    assert result.sample.cycle_count == 3
    assert result.sample.points_count == 4
    assert result.sample.u_eff == pytest.approx(0.0)
    assert result.sample.total_duration_min == pytest.approx(15.0)
    assert result.sample.dTdt == pytest.approx(-0.25 / 3.0)


def test_learning_window_allows_bootstrap_off_cycle_near_setpoint() -> None:
    """A bootstrap cooldown OFF cycle may feed `b` even if estimator-informative is false."""
    result = build_learning_window(
        (
            CycleHistoryEntry(
                tin=22.0,
                tout=10.0,
                target_temp=22.0,
                applied_power=0.0,
                is_valid=True,
                is_informative=True,
                is_estimator_informative=False,
                bootstrap_b_learning_allowed=True,
                cycle_duration_min=10.0,
            ),
            CycleHistoryEntry(
                tin=21.7,
                tout=10.0,
                target_temp=22.0,
                applied_power=0.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=False,
                cycle_duration_min=10.0,
            ),
        ),
        nd_hat=0.0,
        regime=WINDOW_REGIME_OFF,
    )

    assert result.sample is not None
    assert result.reason == "off_window_ready"
    assert result.sample.allow_near_setpoint_b is True
    assert result.sample.setpoint_error == pytest.approx(0.0)


def test_learning_window_reports_dt_per_cycle_not_per_minute() -> None:
    """The learning slope must stay in the discrete per-cycle units used by the controller."""
    result = build_learning_window(
        (
            CycleHistoryEntry(
                tin=20.0,
                tout=10.0,
                target_temp=21.0,
                applied_power=0.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=True,
                cycle_duration_min=5.0,
            ),
            CycleHistoryEntry(
                tin=19.85,
                tout=10.0,
                target_temp=21.0,
                applied_power=0.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=True,
                cycle_duration_min=5.0,
            ),
            CycleHistoryEntry(
                tin=19.7,
                tout=10.0,
                target_temp=21.0,
                applied_power=0.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=True,
                cycle_duration_min=5.0,
            ),
        ),
        nd_hat=0.0,
        regime=WINDOW_REGIME_OFF,
    )

    assert result.sample is not None
    assert result.sample.amplitude == pytest.approx(-0.3)
    assert result.sample.cycle_count == 2
    assert result.sample.total_duration_min == pytest.approx(10.0)
    assert result.sample.dTdt == pytest.approx(-0.15)


def test_learning_window_ignores_recent_on_cycles_when_building_off_window() -> None:
    """OFF learning must search its own latest candidate instead of following the latest ON regime."""
    result = build_learning_window(
        (
            CycleHistoryEntry(
                tin=20.0,
                tout=10.0,
                target_temp=21.0,
                applied_power=0.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=True,
                cycle_duration_min=4.0,
            ),
            CycleHistoryEntry(
                tin=19.7,
                tout=10.0,
                target_temp=21.0,
                applied_power=0.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=True,
                cycle_duration_min=4.0,
            ),
            CycleHistoryEntry(
                tin=19.4,
                tout=10.0,
                target_temp=21.0,
                applied_power=0.6,
                is_valid=True,
                is_informative=True,
                is_estimator_informative=True,
                cycle_duration_min=4.0,
            ),
            CycleHistoryEntry(
                tin=19.55,
                tout=10.0,
                target_temp=21.0,
                applied_power=0.6,
                is_valid=True,
                is_informative=True,
                is_estimator_informative=True,
                cycle_duration_min=4.0,
            ),
        ),
        nd_hat=1.0,
        regime=WINDOW_REGIME_OFF,
    )

    assert result.sample is not None
    assert result.reason == "off_window_ready"
    assert result.sample.cycle_count == 2


def test_learning_window_rejects_setpoint_jump() -> None:
    """A regime-contradicting target change must invalidate the window."""
    result = build_learning_window(
        (
            CycleHistoryEntry(
                tin=20.0,
                tout=10.0,
                target_temp=20.4,
                applied_power=0.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=True,
                cycle_duration_min=4.0,
            ),
            CycleHistoryEntry(
                tin=19.7,
                tout=10.0,
                target_temp=21.0,
                applied_power=0.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=True,
                cycle_duration_min=4.0,
            ),
            CycleHistoryEntry(
                tin=19.45,
                tout=10.0,
                target_temp=20.4,
                applied_power=0.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=True,
                cycle_duration_min=4.0,
            ),
        ),
        nd_hat=1.0,
        regime=WINDOW_REGIME_OFF,
    )

    assert result.sample is None
    assert result.reason == "off_window_setpoint_changed"


def test_learning_window_allows_setpoint_jump_that_reinforces_on_regime() -> None:
    """An upward setpoint jump during heating should not invalidate an ON window."""
    result = build_learning_window(
        (
            CycleHistoryEntry(
                tin=20.0,
                tout=10.0,
                target_temp=21.0,
                applied_power=0.7,
                is_valid=True,
                is_informative=True,
                is_estimator_informative=True,
                cycle_duration_min=4.0,
            ),
            CycleHistoryEntry(
                tin=20.2,
                tout=10.0,
                target_temp=21.5,
                applied_power=0.7,
                is_valid=True,
                is_informative=True,
                is_estimator_informative=True,
                cycle_duration_min=4.0,
            ),
            CycleHistoryEntry(
                tin=20.45,
                tout=10.0,
                target_temp=21.5,
                applied_power=0.7,
                is_valid=True,
                is_informative=True,
                is_estimator_informative=True,
                cycle_duration_min=4.0,
            ),
        ),
        nd_hat=1.0,
        regime=WINDOW_REGIME_ON,
    )

    assert result.sample is not None
    assert result.reason == "on_window_ready"


def test_learning_window_waits_for_more_signal_after_truncating_setpoint_jump_window() -> (
    None
):
    """A truncated post-jump window should wait until enough safe signal is available."""
    result = build_learning_window(
        (
            CycleHistoryEntry(
                tin=20.3,
                tout=10.0,
                target_temp=21.0,
                applied_power=0.6,
                is_valid=True,
                is_informative=True,
                is_estimator_informative=True,
                cycle_duration_min=4.0,
            ),
            CycleHistoryEntry(
                tin=20.0,
                tout=10.0,
                target_temp=20.4,
                applied_power=0.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=True,
                cycle_duration_min=4.0,
            ),
            CycleHistoryEntry(
                tin=19.7,
                tout=10.0,
                target_temp=20.4,
                applied_power=0.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=True,
                cycle_duration_min=4.0,
            ),
            CycleHistoryEntry(
                tin=19.4,
                tout=10.0,
                target_temp=20.4,
                applied_power=0.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=True,
                cycle_duration_min=4.0,
            ),
        ),
        nd_hat=1.0,
        regime=WINDOW_REGIME_OFF,
    )

    assert result.sample is None
    assert result.reason == "off_window_waiting_more_signal"


def test_learning_window_allows_one_safe_cycle_after_setpoint_jump_without_deadtime() -> (
    None
):
    """Without a known deadtime, one full safety cycle after the jump should be enough."""
    result = build_learning_window(
        (
            CycleHistoryEntry(
                tin=20.2,
                tout=10.0,
                target_temp=21.0,
                applied_power=0.7,
                is_valid=True,
                is_informative=True,
                is_estimator_informative=True,
                cycle_duration_min=4.0,
            ),
            CycleHistoryEntry(
                tin=20.0,
                tout=10.0,
                target_temp=20.4,
                applied_power=0.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=True,
                cycle_duration_min=4.0,
            ),
            CycleHistoryEntry(
                tin=19.7,
                tout=10.0,
                target_temp=20.4,
                applied_power=0.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=True,
                cycle_duration_min=4.0,
            ),
            CycleHistoryEntry(
                tin=19.45,
                tout=10.0,
                target_temp=20.4,
                applied_power=0.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=True,
                cycle_duration_min=4.0,
            ),
            CycleHistoryEntry(
                tin=19.35,
                tout=10.0,
                target_temp=20.4,
                applied_power=0.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=True,
                cycle_duration_min=4.0,
            ),
        ),
        nd_hat=0.0,
        regime=WINDOW_REGIME_OFF,
    )

    assert result.sample is not None
    assert result.reason == "off_window_ready"


def test_learning_window_blocks_deadtime_after_regime_transition() -> None:
    """A freshly entered ON regime should stay in blackout for the configured deadtime."""
    result = build_anchored_learning_window(
        (
            CycleHistoryEntry(
                tin=19.0,
                tout=10.0,
                target_temp=20.0,
                applied_power=0.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=True,
                cycle_duration_min=4.0,
            ),
            CycleHistoryEntry(
                tin=18.8,
                tout=10.0,
                target_temp=20.0,
                applied_power=0.7,
                is_valid=True,
                is_informative=True,
                is_estimator_informative=True,
                cycle_duration_min=4.0,
            ),
            CycleHistoryEntry(
                tin=19.0,
                tout=10.0,
                target_temp=20.0,
                applied_power=0.7,
                is_valid=True,
                is_informative=True,
                is_estimator_informative=False,
                cycle_duration_min=4.0,
            ),
        ),
        nd_hat=1.0,
        regime=WINDOW_REGIME_ON,
        end_index=1,
    )

    assert result.sample is None
    assert result.reason == "on_window_deadtime_blackout"
    assert result.deadtime_blackout_active is True


def test_classify_cycle_regime_returns_mixed_for_mid_power() -> None:
    """Intermediate powers should be classified as mixed and not feed A/B directly."""
    assert classify_cycle_regime(0.18) == WINDOW_REGIME_MIXED


def test_learning_window_does_not_restart_blackout_on_mixed_gap_inside_same_regime() -> (
    None
):
    """A mixed cycle between two OFF cycles should not count as a fresh OFF transition."""
    result = build_anchored_learning_window(
        (
            CycleHistoryEntry(
                tin=20.4,
                tout=10.0,
                target_temp=20.0,
                applied_power=0.7,
                is_valid=True,
                is_informative=True,
                is_estimator_informative=True,
                cycle_duration_min=4.0,
            ),
            CycleHistoryEntry(
                tin=20.2,
                tout=10.0,
                target_temp=20.0,
                applied_power=0.05,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=True,
                cycle_duration_min=4.0,
            ),
            CycleHistoryEntry(
                tin=20.0,
                tout=10.0,
                target_temp=20.0,
                applied_power=0.18,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=True,
                cycle_duration_min=4.0,
            ),
            CycleHistoryEntry(
                tin=19.8,
                tout=10.0,
                target_temp=20.0,
                applied_power=0.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=True,
                cycle_duration_min=4.0,
            ),
            CycleHistoryEntry(
                tin=19.6,
                tout=10.0,
                target_temp=20.0,
                applied_power=0.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=False,
                cycle_duration_min=4.0,
            ),
        ),
        nd_hat=1.0,
        regime=WINDOW_REGIME_OFF,
        end_index=3,
    )

    assert result.sample is not None
    assert result.reason == "off_window_ready"
    assert result.deadtime_blackout_active is False


def test_learning_window_rejects_off_window_when_temperature_rises() -> None:
    """OFF windows with a positive thermal drift should be rejected explicitly."""
    result = build_learning_window(
        (
            CycleHistoryEntry(
                tin=19.0,
                tout=10.0,
                target_temp=21.0,
                applied_power=0.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=True,
                cycle_duration_min=5.0,
            ),
            CycleHistoryEntry(
                tin=19.2,
                tout=10.0,
                target_temp=21.0,
                applied_power=0.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=True,
                cycle_duration_min=5.0,
            ),
            CycleHistoryEntry(
                tin=19.3,
                tout=10.0,
                target_temp=21.0,
                applied_power=0.0,
                is_valid=True,
                is_informative=False,
                is_estimator_informative=True,
                cycle_duration_min=5.0,
            ),
        ),
        nd_hat=0.0,
        regime=WINDOW_REGIME_OFF,
    )

    assert result.sample is None
    assert result.reason == "off_window_no_thermal_loss"


def test_learning_window_rejects_on_window_when_temperature_drops() -> None:
    """ON windows with no positive thermal response should be rejected explicitly."""
    result = build_learning_window(
        (
            CycleHistoryEntry(
                tin=20.0,
                tout=10.0,
                target_temp=21.0,
                applied_power=0.8,
                is_valid=True,
                is_informative=True,
                is_estimator_informative=True,
                cycle_duration_min=5.0,
            ),
            CycleHistoryEntry(
                tin=19.8,
                tout=10.0,
                target_temp=21.0,
                applied_power=0.8,
                is_valid=True,
                is_informative=True,
                is_estimator_informative=True,
                cycle_duration_min=5.0,
            ),
            CycleHistoryEntry(
                tin=19.7,
                tout=10.0,
                target_temp=21.0,
                applied_power=0.8,
                is_valid=True,
                is_informative=True,
                is_estimator_informative=True,
                cycle_duration_min=5.0,
            ),
        ),
        nd_hat=0.0,
        regime=WINDOW_REGIME_ON,
    )

    assert result.sample is None
    assert result.reason == "on_window_no_actuator_effect"


def test_estimator_can_learn_b_on_zero_power_cycles() -> None:
    """Cooling-only informative cycles should update `b_hat` without moving `a_hat`."""
    estimator = ParameterEstimator()
    initial_a_hat = estimator.a_hat
    initial_b_hat = estimator.b_hat

    update = estimator.update_b(
        BSample(
            dTdt=-0.06,
            delta_out=8.0,
            setpoint_error=1.0,
            u_eff=0.0,
        )
    )

    assert update.updated is True
    assert update.a_hat == pytest.approx(initial_a_hat)
    assert update.b_hat > initial_b_hat
    assert update.b_last_reason == "sample_accepted"


def test_estimator_can_learn_b_near_setpoint_during_bootstrap_cooldown() -> None:
    """Bootstrap cooldown may feed `b` even when the cycle started near setpoint."""
    estimator = ParameterEstimator()

    update = estimator.update_b(
        BSample(
            dTdt=-0.06,
            delta_out=8.0,
            setpoint_error=0.0,
            u_eff=0.0,
            allow_near_setpoint_b=True,
        )
    )

    assert update.updated is True
    assert update.b_hat > 0.0
    assert update.b_last_reason == "sample_accepted"


def test_a_does_not_move_until_b_has_converged() -> None:
    """The heating gain must stay frozen until the loss estimate is stable enough."""
    estimator = ParameterEstimator()

    update = estimator.update_a(
        ASample(
            dTdt=0.04,
            delta_out=8.0,
            setpoint_error=1.0,
            u_eff=0.6,
        )
    )

    assert update.updated is False
    assert update.a_hat == pytest.approx(A_MIN)
    assert update.a_last_reason == "a_waiting_b_converged"


def test_a_starts_learning_after_b_stabilization() -> None:
    """The heating gain may start learning once `b` has converged on OFF cycles."""
    estimator = ParameterEstimator()

    for _ in range(4):
        estimator.update_b(
            BSample(
                dTdt=-0.08,
                delta_out=8.0,
                setpoint_error=1.0,
                u_eff=0.0,
            )
        )

    update = estimator.update_a(
        ASample(
            dTdt=0.05,
            delta_out=8.0,
            setpoint_error=1.0,
            u_eff=0.6,
        )
    )

    assert update.b_converged is True
    assert update.updated is True
    assert update.a_hat > A_MIN


def test_non_informative_cycle_skips_estimator_update() -> None:
    """Low-excitation accepted cycles should be classified as non-informative."""
    algo = AdaptiveTPIAlgorithm(name="test-non-informative", debug_mode=True)

    for _ in range(2):
        algo.calculate(
            target_temp=20.0,
            current_temp=19.95,
            ext_current_temp=19.5,
            slope=None,
            hvac_mode="heat",
            cycle_min=5.0,
        )
        algo.on_cycle_started(
            on_time_sec=0.0,
            off_time_sec=300.0,
            on_percent=0.0,
            hvac_mode="heat",
            target_temp=20.0,
            current_temp=19.95,
            ext_current_temp=19.5,
        )
        algo.on_cycle_completed(
            e_eff=0.0,
            elapsed_ratio=1.0,
            cycle_duration_min=5.0,
            target_temp=20.0,
            current_temp=19.95,
            ext_current_temp=19.5,
            hvac_mode="heat",
        )

    diagnostics = algo.get_diagnostics()
    assert diagnostics["last_runtime_blocker"] == "non_informative_cycle"
    assert diagnostics["debug"]["last_cycle_classification"] == "non_informative"
    assert diagnostics["debug"]["accepted_cycles_count"] == 2


def test_completed_on_cycle_routes_to_a_not_b(monkeypatch: pytest.MonkeyPatch) -> None:
    """A completed ON cycle must not fall back to the OFF branch."""
    algo = AdaptiveTPIAlgorithm(name="test-route-a", debug_mode=True)

    def fake_record_cycle(observation, **kwargs):
        algo._deadtime_model._cycle_history.append(
            CycleHistoryEntry(
                tin=observation.tin,
                tout=observation.tout,
                target_temp=observation.target_temp,
                applied_power=observation.applied_power,
                is_valid=kwargs["is_valid"],
                is_informative=kwargs["is_informative"],
                is_estimator_informative=kwargs["is_estimator_informative"],
                cycle_duration_min=kwargs["cycle_duration_min"],
            )
        )
        return DeadtimeSearchResult(
            nd_hat=0.0,
            nd_minutes=None,
            c_nd=1.0,
            locked=True,
            best_candidate=0.0,
            second_best_candidate=1.0,
            best_candidate_a=0.3,
            best_candidate_b=0.02,
            candidate_costs={"0": 0.01, "1": 0.03},
            lock_reason=None,
        )

    monkeypatch.setattr(algo._deadtime_model, "record_cycle", fake_record_cycle)
    monkeypatch.setattr(
        algo._supervisor,
        "allow_a_update",
        lambda **kwargs: True,
    )

    for _ in range(4):
        algo._estimator.update_b(
            BSample(
                dTdt=-0.08,
                delta_out=8.0,
                setpoint_error=1.0,
                u_eff=0.0,
            )
        )
    algo._state.b_converged = True

    algo.on_cycle_started(
        on_time_sec=600.0,
        off_time_sec=0.0,
        on_percent=0.8,
        hvac_mode="heat",
        target_temp=21.0,
        current_temp=20.0,
        ext_current_temp=10.0,
    )
    algo.on_cycle_completed(
        e_eff=0.8,
        elapsed_ratio=1.0,
        cycle_duration_min=10.0,
        target_temp=21.0,
        current_temp=20.3,
        ext_current_temp=10.0,
        hvac_mode="heat",
    )

    diagnostics = algo.get_diagnostics()
    assert diagnostics["debug"]["current_cycle_regime"] == "on"
    assert diagnostics["debug"]["learning_route_selected"] == "a"
    assert diagnostics["last_learning_family"] == "control"
    assert diagnostics["control_samples"] >= 1


def test_deadtime_history_uses_realized_cycle_power(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deadtime history must use the effective power delivered over the cycle."""
    algo = AdaptiveTPIAlgorithm(name="test-deadtime-cycle-power", debug_mode=True)
    recorded_powers: list[float] = []

    def fake_record_cycle(observation, **kwargs):
        recorded_powers.append(observation.applied_power)
        return DeadtimeSearchResult(
            nd_hat=0.0,
            nd_minutes=None,
            c_nd=0.0,
            locked=False,
            best_candidate=None,
            second_best_candidate=None,
            best_candidate_a=None,
            best_candidate_b=None,
            candidate_costs={},
            lock_reason="deadtime_insufficient_identifications",
        )

    monkeypatch.setattr(algo._deadtime_model, "record_cycle", fake_record_cycle)

    algo.on_cycle_started(
        on_time_sec=600.0,
        off_time_sec=0.0,
        on_percent=0.8,
        hvac_mode="heat",
        target_temp=21.0,
        current_temp=20.0,
        ext_current_temp=10.0,
    )
    algo.on_cycle_completed(
        e_eff=0.2,
        elapsed_ratio=1.0,
        cycle_duration_min=10.0,
        target_temp=21.0,
        current_temp=20.2,
        ext_current_temp=10.0,
        hvac_mode="heat",
    )

    diagnostics = algo.get_diagnostics()
    assert recorded_powers == [pytest.approx(0.2)]
    assert diagnostics["debug"]["current_cycle_regime"] == "mixed"
    assert algo._state.committed_on_percent == pytest.approx(0.8)


def test_calculate_keeps_committed_power_separate_from_next_requested_power() -> None:
    """Requested power should not overwrite the currently committed cycle power."""
    algo = AdaptiveTPIAlgorithm(name="test-requested-vs-committed", debug_mode=True)

    algo.on_cycle_started(
        on_time_sec=180.0,
        off_time_sec=120.0,
        on_percent=0.6,
        hvac_mode="heat",
        target_temp=21.0,
        current_temp=20.0,
        ext_current_temp=10.0,
    )
    algo.calculate(
        target_temp=22.0,
        current_temp=20.0,
        ext_current_temp=10.0,
        slope=None,
        hvac_mode="heat",
    )

    diagnostics = algo.get_diagnostics()
    assert algo._state.committed_on_percent == pytest.approx(0.6)
    assert algo.requested_on_percent == pytest.approx(1.0)
    assert diagnostics["current_cycle_percent"] == pytest.approx(0.6)
    assert diagnostics["next_cycle_percent"] == pytest.approx(1.0)
    assert diagnostics["debug"]["committed_on_percent"] == pytest.approx(0.6)
    assert diagnostics["debug"]["requested_on_percent"] == pytest.approx(1.0)


def test_bootstrap_cooldown_off_cycle_can_update_b_near_setpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bootstrap cooldown OFF cycle should be allowed to feed `b` near setpoint."""
    algo = AdaptiveTPIAlgorithm(name="test-bootstrap-b-cooldown", debug_mode=True)

    def fake_record_cycle(observation, **kwargs):
        algo._deadtime_model._cycle_history.append(
            CycleHistoryEntry(
                tin=observation.tin,
                tout=observation.tout,
                target_temp=observation.target_temp,
                applied_power=observation.applied_power,
                is_valid=kwargs["is_valid"],
                is_informative=kwargs["is_informative"],
                is_estimator_informative=kwargs["is_estimator_informative"],
                bootstrap_b_learning_allowed=kwargs["bootstrap_b_learning_allowed"],
                cycle_duration_min=kwargs["cycle_duration_min"],
            )
        )
        return DeadtimeSearchResult(
            nd_hat=0.0,
            nd_minutes=None,
            c_nd=0.0,
            locked=False,
            best_candidate=None,
            second_best_candidate=None,
            best_candidate_a=None,
            best_candidate_b=None,
            candidate_costs={},
            lock_reason="deadtime_insufficient_identifications",
        )

    monkeypatch.setattr(algo._deadtime_model, "record_cycle", fake_record_cycle)

    algo.calculate(
        target_temp=22.0,
        current_temp=22.0,
        ext_current_temp=10.0,
        slope=None,
        hvac_mode="heat",
    )
    algo.on_cycle_started(
        on_time_sec=0.0,
        off_time_sec=600.0,
        on_percent=0.0,
        hvac_mode="heat",
        target_temp=22.0,
        current_temp=22.0,
        ext_current_temp=10.0,
    )
    algo.on_cycle_completed(
        e_eff=0.0,
        elapsed_ratio=1.0,
        cycle_duration_min=10.0,
        target_temp=22.0,
        current_temp=21.7,
        ext_current_temp=10.0,
        hvac_mode="heat",
    )

    diagnostics = algo.get_diagnostics()
    assert diagnostics["debug"]["learning_route_selected"] == "b"
    assert diagnostics["last_learning_result"] == "sample_accepted"
    assert diagnostics["debug"]["b_last_reason"] == "sample_accepted"
    assert diagnostics["drift_samples"] >= 1
    assert diagnostics["debug"]["b_hat"] > 0.0


def test_disturbed_cycle_freezes_adaptation() -> None:
    """Disturbed runtime conditions should reject the cycle with an explicit reason."""
    algo = AdaptiveTPIAlgorithm(name="test-disturbed", debug_mode=True)

    algo.calculate(
        target_temp=21.0,
        current_temp=20.0,
        ext_current_temp=8.0,
        slope=None,
        hvac_mode="heat",
        power_shedding=True,
    )
    algo.on_cycle_started(
        on_time_sec=180.0,
        off_time_sec=120.0,
        on_percent=0.6,
        hvac_mode="heat",
        target_temp=21.0,
        current_temp=20.0,
        ext_current_temp=8.0,
    )
    algo.on_cycle_completed(
        e_eff=0.6,
        elapsed_ratio=1.0,
        cycle_duration_min=5.0,
        target_temp=21.0,
        current_temp=20.0,
        ext_current_temp=8.0,
        hvac_mode="heat",
        power_shedding=True,
    )

    diagnostics = algo.get_diagnostics()
    assert diagnostics["last_runtime_blocker"] == "power_shedding"
    assert diagnostics["debug"]["last_cycle_classification"] == "rejected"
    assert diagnostics["debug"]["accepted_cycles_count"] == 0


def test_gain_projection_matches_structural_formulas() -> None:
    """Gain targets and bounded projection should follow the math spec."""
    k_int_target, k_ext_target = compute_gain_targets(
        a_hat=0.2,
        b_hat=0.03,
        nd_hat=2.0,
    )

    # tau_cl = max(3.0, 2.0*2.0) = 4.0, lambda_cl = exp(-0.25)
    assert k_int_target == pytest.approx((1.0 - math.exp(-0.25) - 0.03) / 0.2, rel=1e-4)
    assert k_ext_target == pytest.approx(0.15)

    next_k_int, next_k_ext = project_gains(
        phase=PHASE_C,
        k_int=0.2,
        k_ext=0.01,
        a_hat=0.2,
        b_hat=0.03,
        nd_hat=2.0,
        c_nd=0.8,
        c_a=0.8,
        c_b=0.8,
    )
    assert next_k_int == pytest.approx(0.23)
    assert next_k_ext == pytest.approx(0.015)


def test_gain_targets_lambda_follows_deadtime() -> None:
    """Floor is active for nd<=1; higher deadtime detunes the loop (lower Kint)."""
    a, b = 0.2, 0.03
    k_int_nd0, _ = compute_gain_targets(a_hat=a, b_hat=b, nd_hat=0.0)
    k_int_nd1, _ = compute_gain_targets(a_hat=a, b_hat=b, nd_hat=1.0)
    k_int_nd4, _ = compute_gain_targets(a_hat=a, b_hat=b, nd_hat=4.0)
    # Both nd=0 and nd=1 hit the floor tau_cl=3 -> same lambda_cl -> same Kint
    assert k_int_nd0 == pytest.approx(k_int_nd1, rel=1e-9)
    # Larger deadtime -> larger tau_cl -> larger lambda_cl -> smaller Kint
    assert k_int_nd4 < k_int_nd0


def test_gain_targets_kext_is_ratio() -> None:
    """Kext_target equals b_hat/a_hat across a grid of inputs."""
    cases = [
        (0.2, 0.03, 0.0),
        (0.1, 0.05, 2.0),
        (0.05, 0.01, 4.0),
        (0.3, 0.0, 1.0),
    ]
    for a, b, nd in cases:
        _, k_ext = compute_gain_targets(a_hat=a, b_hat=b, nd_hat=nd)
        assert k_ext == pytest.approx(b / a, rel=1e-9)


def test_algo_exposes_deadtime_b_proxy_and_crosscheck_after_bootstrap_seed() -> None:
    """The runtime should surface the deadtime-side `b` proxy and seed `b_hat` from it."""
    algo = AdaptiveTPIAlgorithm(name="test-deadtime-b-proxy", debug_mode=True)

    for i in range(3):
        algo._deadtime_model._identifications.append(
            StepIdentification(
                nd_cycles=2.0 + 0.05 * i, quality=0.85, b_proxy=0.03, cycle_index=i * 30
            )
        )

    result = algo._deadtime_model.evaluate()
    algo._state.deadtime_b_proxy = result.best_candidate_b
    algo._apply_estimator_update(
        algo._estimator.seed_b_from_deadtime_proxy(result.best_candidate_b)
    )
    algo._refresh_b_crosscheck()

    diagnostics = algo.get_diagnostics()
    assert diagnostics["debug"]["deadtime_b_proxy"] == pytest.approx(result.best_candidate_b)
    assert diagnostics["debug"]["b_hat"] == pytest.approx(result.best_candidate_b)
    assert diagnostics["debug"]["b_crosscheck_error"] == pytest.approx(0.0)
    assert diagnostics["debug"]["b_methods_consistent"] is True


def test_gain_projection_keeps_bootstrap_defaults_while_confidence_is_low() -> None:
    """Low-confidence degraded mode should freeze the last computed gains."""
    next_k_int, next_k_ext = project_gains(
        phase=PHASE_B,
        k_int=1.2,
        k_ext=0.0,
        a_hat=0.001,
        b_hat=0.0,
        nd_hat=0.0,
        c_nd=0.1,
        c_a=0.0,
        c_b=0.0,
    )

    assert next_k_int == pytest.approx(1.2)
    assert next_k_ext == pytest.approx(0.0)


def test_calculate_does_not_adapt_gains_on_sensor_refresh() -> None:
    """Repeated calculate calls must not move gains outside cycle callbacks."""
    algo = AdaptiveTPIAlgorithm(name="test-refresh-stability", debug_mode=True)
    algo._state.k_int = 0.72
    algo._state.k_ext = 0.04
    algo._state.bootstrap_phase = PHASE_C
    algo._state.a_hat = 0.2
    algo._state.b_hat = 0.03
    algo._state.nd_hat = 2.0
    algo._state.c_nd = 0.8
    algo._state.c_a = 0.8
    algo._state.c_b = 0.8

    for _ in range(5):
        algo.calculate(
            target_temp=21.0,
            current_temp=20.0,
            ext_current_temp=8.0,
            slope=None,
            hvac_mode="heat",
        )

    diagnostics = algo.get_diagnostics()
    assert diagnostics["gain_indoor"] == pytest.approx(0.72)
    assert diagnostics["gain_outdoor"] == pytest.approx(0.04)


def test_reset_learning_restores_fresh_bootstrap_defaults() -> None:
    """A manual reset should clear all learned runtime state."""
    algo = AdaptiveTPIAlgorithm(name="test-reset", debug_mode=True)
    algo._state.k_int = 1.2
    algo._state.k_ext = 0.0
    algo._state.nd_hat = 2.0
    algo._state.c_nd = 0.7
    algo._state.a_hat = 0.12
    algo._state.b_hat = 0.02
    algo._state.c_a = 0.6
    algo._state.c_b = 0.5
    algo._state.accepted_cycles_count = 12
    algo._state.valid_cycles_count = 12
    algo._state.bootstrap_phase = "phase_c"

    algo.reset_learning()

    diagnostics = algo.get_diagnostics()
    assert diagnostics["adaptive_phase"] == "startup"
    assert diagnostics["gain_indoor"] == pytest.approx(0.6)
    assert diagnostics["gain_outdoor"] == pytest.approx(0.02)
    assert diagnostics["deadtime_cycles"] == pytest.approx(0.0)
    assert diagnostics["deadtime_confidence"] == pytest.approx(0.0)
    assert diagnostics["debug"]["a_hat"] == pytest.approx(0.001)
    assert diagnostics["debug"]["b_hat"] == pytest.approx(0.0)
    assert diagnostics["debug"]["accepted_cycles_count"] == 0
    assert diagnostics["debug"]["deadtime_locked"] is False


def test_cycle_min_change_converts_persisted_warm_start() -> None:
    """A cycle duration change should preserve learned state in the new cycle units."""
    algo = AdaptiveTPIAlgorithm(name="test-persistence", debug_mode=True)
    algo._state.bootstrap_phase = PHASE_D
    algo._state.cycle_min_at_last_accepted_cycle = 5.0

    algo._deadtime_model._identifications.append(
        StepIdentification(
            nd_cycles=2.0,
            nd_minutes=10.0,
            quality=0.85,
            b_proxy=0.02,
            cycle_index=3,
        )
    )
    algo._deadtime_model.evaluate()

    for measurement in (0.0200, 0.0210, 0.0220, 0.0230, 0.0225, 0.0215):
        algo._estimator._b_estimator.push(measurement)
    algo._estimator.b_hat = algo._estimator._b_estimator.estimate
    algo._estimator.c_b = algo._estimator._b_estimator.confidence
    algo._estimator.b_converged = algo._estimator._compute_b_converged()

    for measurement in (0.100, 0.110, 0.120, 0.115, 0.105, 0.125):
        algo._estimator._a_estimator.push(measurement)
    algo._estimator.a_hat = algo._estimator._a_estimator.estimate
    algo._estimator.c_a = algo._estimator._a_estimator.confidence

    deadtime_result = algo._deadtime_model.last_result
    algo._state.nd_hat = deadtime_result.nd_hat
    algo._state.deadtime_minutes = deadtime_result.nd_minutes
    algo._state.c_nd = deadtime_result.c_nd
    algo._state.deadtime_locked = deadtime_result.locked
    algo._state.deadtime_best_candidate = deadtime_result.best_candidate
    algo._state.deadtime_second_best_candidate = deadtime_result.second_best_candidate
    algo._state.deadtime_b_proxy = deadtime_result.best_candidate_b
    algo._state.deadtime_identification_count = len(
        algo._deadtime_model._identifications
    )
    algo._state.deadtime_identification_qualities = deadtime_result.candidate_costs
    algo._state.a_hat = algo._estimator.a_hat
    algo._state.b_hat = algo._estimator.b_hat
    algo._state.c_a = algo._estimator.c_a
    algo._state.c_b = algo._estimator.c_b
    algo._state.b_converged = algo._estimator.b_converged

    saved = algo.save_state(cycle_min=5.0)
    assert saved["a_hat_per_hour"] == pytest.approx(algo._state.a_hat * 12.0)
    assert saved["b_hat_per_hour"] == pytest.approx(algo._state.b_hat * 12.0)
    assert saved["deadtime_model"]["identifications"][0]["nd_minutes"] == pytest.approx(
        10.0
    )
    assert saved["deadtime_model"]["identifications"][0][
        "b_proxy_per_hour"
    ] == pytest.approx(0.24)
    restored = AdaptiveTPIAlgorithm(name="test-persistence-restore", debug_mode=True)
    restored.load_state(saved, current_cycle_min=10.0, persisted_cycle_min=5.0)

    diagnostics = restored.get_diagnostics()
    assert diagnostics["adaptive_phase"] == "stabilized"
    assert diagnostics["deadtime_cycles"] == pytest.approx(1.0)
    assert diagnostics["deadtime_minutes"] == pytest.approx(10.0)
    assert diagnostics["control_samples"] == 6
    assert diagnostics["drift_samples"] == 6
    assert diagnostics["debug"]["a_hat"] == pytest.approx(algo._state.a_hat * 2.0)
    assert diagnostics["debug"]["b_hat"] == pytest.approx(algo._state.b_hat * 2.0)
    assert diagnostics["debug"]["deadtime_b_proxy"] == pytest.approx(0.04)
    assert diagnostics["debug"]["cycle_min_at_last_accepted_cycle"] == pytest.approx(
        10.0
    )
    assert diagnostics["debug"]["deadtime_locked"] is True
    assert diagnostics["last_runtime_blocker"] is None


def test_warm_start_restores_deadtime_model_and_candidate_costs() -> None:
    """A normal warm start should preserve the deadtime model, not only the summary state."""
    algo = AdaptiveTPIAlgorithm(name="test-deadtime-persistence", debug_mode=True)

    for i in range(3):
        algo._deadtime_model._identifications.append(
            StepIdentification(
                nd_cycles=2.0 + 0.05 * i, quality=0.85, b_proxy=0.04, cycle_index=i * 30
            )
        )
    algo._deadtime_model.evaluate()

    result = algo._deadtime_model.last_result
    algo._state.nd_hat = result.nd_hat
    algo._state.c_nd = result.c_nd
    algo._state.deadtime_locked = result.locked
    algo._state.deadtime_best_candidate = result.best_candidate
    algo._state.deadtime_second_best_candidate = result.second_best_candidate
    algo._state.deadtime_identification_qualities = result.candidate_costs
    algo._state.deadtime_b_proxy = result.best_candidate_b
    algo._state.bootstrap_phase = PHASE_C
    algo._state.a_hat = 0.2
    algo._state.b_hat = 0.03
    algo._state.c_a = 0.8
    algo._state.c_b = 0.8

    saved = algo.save_state(cycle_min=5.0)

    restored = AdaptiveTPIAlgorithm(
        name="test-deadtime-persistence-restore", debug_mode=True
    )
    restored.load_state(
        saved,
        current_cycle_min=5.0,
        persisted_cycle_min=5.0,
    )

    diagnostics = restored.get_diagnostics()
    assert diagnostics["deadtime_cycles"] == pytest.approx(result.nd_hat)
    assert diagnostics["deadtime_confidence"] == pytest.approx(result.c_nd)
    assert diagnostics["debug"]["deadtime_identification_qualities"] == result.candidate_costs
    assert diagnostics["debug"]["deadtime_b_proxy"] == pytest.approx(result.best_candidate_b)
    assert diagnostics["debug"]["deadtime_best_candidate"] == pytest.approx(
        result.best_candidate
    )


def test_warm_start_after_long_gap_keeps_locked_deadtime() -> None:
    """A long warm-start gap must not clear a previously locked deadtime."""
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    algo = AdaptiveTPIAlgorithm(name="test-long-warm-start", debug_mode=True)
    algo._utc_now = lambda: now
    algo._deadtime_model.record_identification(
        StepIdentification(
            nd_cycles=2.0,
            nd_minutes=10.0,
            quality=0.9,
            b_proxy=0.04,
            cycle_index=1,
            transition="on",
        )
    )
    result = algo._deadtime_model.last_result
    algo._state.nd_hat = result.nd_hat
    algo._state.deadtime_minutes = result.nd_minutes
    algo._state.c_nd = result.c_nd
    algo._state.deadtime_locked = result.locked
    algo._state.deadtime_on_cycles = result.nd_hat_on
    algo._state.deadtime_on_minutes = result.nd_minutes_on
    algo._state.deadtime_on_confidence = result.c_nd_on
    algo._state.deadtime_on_locked = result.deadtime_on_locked
    algo._state.bootstrap_phase = PHASE_C
    algo._state.a_hat = 0.2
    algo._state.b_hat = 0.03
    algo._state.c_a = 0.8
    algo._state.c_b = 0.8

    saved = algo.save_state(cycle_min=5.0)
    restored = AdaptiveTPIAlgorithm(name="test-long-warm-start-restored", debug_mode=True)
    restored._utc_now = lambda: now
    restored.load_state(
        saved,
        current_cycle_min=5.0,
        persisted_cycle_min=5.0,
        saved_at=(now - timedelta(days=120)).isoformat(),
    )

    diagnostics = restored.get_diagnostics()
    assert diagnostics["deadtime_cycles"] == pytest.approx(2.0)
    assert diagnostics["deadtime_minutes"] == pytest.approx(10.0)
    assert diagnostics["deadtime_confidence"] == pytest.approx(0.9)
    assert diagnostics["debug"]["deadtime_locked"] is True
    assert diagnostics["debug"]["deadtime_on_locked"] is True


def test_warm_start_restores_estimator_history_and_keeps_adaptive_gains() -> None:
    """A warm start should preserve estimator confidence and the last adaptive gains."""
    algo = AdaptiveTPIAlgorithm(name="test-estimator-persistence", debug_mode=True)
    algo._state.bootstrap_phase = PHASE_C

    b_samples = (0.0200, 0.0210, 0.0220, 0.0230, 0.0225, 0.0215)
    for measurement in b_samples:
        algo._estimator._b_estimator.push(measurement)

    algo._estimator.b_hat = algo._estimator._b_estimator.estimate
    algo._estimator.c_b = algo._estimator._b_estimator.confidence
    algo._estimator.b_converged = algo._estimator._compute_b_converged()

    a_samples = (0.740, 0.755, 0.765, 0.775, 0.760, 0.770)
    for measurement in a_samples:
        algo._estimator._a_estimator.push(measurement)

    algo._estimator.a_hat = algo._estimator._a_estimator.estimate
    algo._estimator.c_a = algo._estimator._a_estimator.confidence

    algo._state.a_hat = algo._estimator.a_hat
    algo._state.b_hat = algo._estimator.b_hat
    algo._state.c_a = algo._estimator.c_a
    algo._state.c_b = algo._estimator.c_b
    algo._state.b_converged = algo._estimator.b_converged
    algo._state.nd_hat = 0.0
    algo._state.c_nd = 2.0 / 3.0
    algo._state.k_int = 0.42
    algo._state.k_ext = 0.03

    saved = algo.save_state(cycle_min=5.0)
    assert saved["estimator_model"]["a_estimator"]["samples_per_hour"] == pytest.approx(
        [sample * 12.0 for sample in a_samples]
    )
    assert saved["estimator_model"]["b_estimator"]["samples_per_hour"] == pytest.approx(
        [sample * 12.0 for sample in b_samples]
    )

    restored = AdaptiveTPIAlgorithm(
        name="test-estimator-persistence-restore", debug_mode=True
    )
    restored.load_state(
        saved,
        current_cycle_min=5.0,
        persisted_cycle_min=5.0,
    )

    diagnostics = restored.get_diagnostics()
    assert diagnostics["control_samples"] == len(a_samples)
    assert diagnostics["drift_samples"] == len(b_samples)
    assert diagnostics["sample_window_size"] == 12
    assert diagnostics["debug"]["sample_window_size"] == 12
    assert diagnostics["control_rate_confidence"] == pytest.approx(algo._state.c_a)
    assert diagnostics["drift_rate_confidence"] == pytest.approx(algo._state.c_b)
    assert diagnostics["control_rate_converged"] is True
    assert diagnostics["drift_rate_converged"] is True
    assert diagnostics["debug"]["control_rate_converged"] is True
    assert diagnostics["gain_indoor"] != pytest.approx(0.6)
    assert diagnostics["gain_outdoor"] != pytest.approx(0.01)


def test_load_state_reapplies_configured_valve_curve_when_learning_disabled() -> None:
    """Configured valve parameters must win over persisted learned parameters when learning is disabled."""
    configured_params = ValveCurveParams(
        min_valve=12.0,
        knee_demand=75.0,
        knee_valve=24.0,
        max_valve=95.0,
    )
    algo = AdaptiveTPIAlgorithm(
        name="test-valve-config-precedence",
        actuator_mode=ACTUATOR_MODE_VALVE,
        valve_curve_params=configured_params,
        valve_curve_learning_enabled=False,
        debug_mode=True,
    )

    algo.load_state(
        {
            "valve_curve": {
                "actuator_mode": ACTUATOR_MODE_VALVE,
                "params": {
                    "min_valve": 7.0,
                    "knee_demand": 80.0,
                    "knee_valve": 15.0,
                    "max_valve": 100.0,
                },
                "configured_params": {
                    "min_valve": 7.0,
                    "knee_demand": 80.0,
                    "knee_valve": 15.0,
                    "max_valve": 100.0,
                },
                "learning_enabled": True,
                "observations": [
                    {
                        "u_linear_equiv": 0.8,
                        "u_valve": 0.2,
                        "timestamp": "2026-04-24T12:00:00+00:00",
                    }
                ],
                "observations_accepted_count": 1,
            }
        }
    )

    diagnostics = algo.get_diagnostics()
    assert diagnostics["valve_curve_params"] == {
        "min_valve": pytest.approx(12.0),
        "knee_demand": pytest.approx(75.0),
        "knee_valve": pytest.approx(24.0),
        "max_valve": pytest.approx(95.0),
    }
    assert diagnostics["valve_curve_learning_enabled"] is False
    assert diagnostics["valve_curve_observations_accepted"] == 0


def test_bootstrap_stuck_exposes_explicit_freeze_reason() -> None:
    """Repeated low-information cycles should trip the bootstrap stuck guard."""
    algo = AdaptiveTPIAlgorithm(name="test-bootstrap-stuck", debug_mode=True)

    for _ in range(10):
        algo.calculate(
            target_temp=20.0,
            current_temp=19.95,
            ext_current_temp=19.5,
            slope=None,
            hvac_mode="heat",
            cycle_min=5.0,
        )
        algo.on_cycle_started(
            on_time_sec=0.0,
            off_time_sec=300.0,
            on_percent=0.0,
            hvac_mode="heat",
            target_temp=20.0,
            current_temp=19.95,
            ext_current_temp=19.5,
        )
        algo.on_cycle_completed(
            e_eff=0.0,
            elapsed_ratio=1.0,
            cycle_duration_min=5.0,
            target_temp=20.0,
            current_temp=19.95,
            ext_current_temp=19.5,
            hvac_mode="heat",
        )

    diagnostics = algo.get_diagnostics()
    assert diagnostics["adaptive_phase"] == "deadtime_learning"
    assert diagnostics["debug"]["accepted_cycles_count"] == 10
    assert diagnostics["debug"]["hours_without_excitation"] == pytest.approx(10 * 5.0 / 60.0)
    assert diagnostics["last_runtime_blocker"] == "insufficient_excitation_bootstrap"


# ---------------------------------------------------------------------------
# MAD outlier guard tests
# ---------------------------------------------------------------------------

# Helper: build 5 stable b samples around 0.10 with non-zero MAD.
# measurement = -(dTdt / delta_out); using delta_out=8.0 gives measurements
# [0.08, 0.09, 0.10, 0.11, 0.12], center=0.10, MAD=0.01.
_STABLE_B_DTDTS = [-0.64, -0.72, -0.80, -0.88, -0.96]

# Helper: build stable a samples around 0.50 given b_hat≈0.10, delta_out=8, u_eff=0.8.
# measurement = (dTdt + 0.80) / 0.8; the five dTdts below yield [0.48..0.52].
_STABLE_A_DTDTS = [-0.416, -0.408, -0.400, -0.392, -0.384]


def _build_stable_b(estimator: ParameterEstimator) -> None:
    for dTdt in _STABLE_B_DTDTS:
        estimator.update_b(BSample(dTdt=dTdt, delta_out=8.0, setpoint_error=1.5, u_eff=0.0))


def _build_stable_a(estimator: ParameterEstimator) -> None:
    for dTdt in _STABLE_A_DTDTS:
        estimator.update_a(ASample(dTdt=dTdt, delta_out=8.0, setpoint_error=1.5, u_eff=0.8))


def test_b_mad_outlier_is_rejected() -> None:
    """A single aberrant b measurement is rejected when the estimator has a stable baseline."""
    estimator = ParameterEstimator()
    _build_stable_b(estimator)

    before = estimator.update_b(BSample(dTdt=-0.80, delta_out=8.0, setpoint_error=1.5, u_eff=0.0))
    b_hat_before = before.b_hat
    c_b_before = before.c_b
    count_before = before.b_samples_count

    # measurement = 3.2 / 8.0 = 0.40 — robust_z ≈ 20 >> 4.5
    outlier = estimator.update_b(BSample(dTdt=-3.2, delta_out=8.0, setpoint_error=1.5, u_eff=0.0))

    assert outlier.b_updated is False
    assert outlier.updated is False
    assert outlier.b_last_reason == "b_measurement_outlier_mad"
    assert outlier.b_hat == pytest.approx(b_hat_before)
    assert outlier.c_b == pytest.approx(c_b_before)
    assert outlier.b_samples_count == count_before


def test_a_mad_outlier_is_rejected() -> None:
    """A single aberrant a measurement is rejected when the estimator has a stable baseline."""
    estimator = ParameterEstimator()
    _build_stable_b(estimator)
    _build_stable_a(estimator)

    before = estimator.update_a(ASample(dTdt=-0.400, delta_out=8.0, setpoint_error=1.5, u_eff=0.8))
    a_hat_before = before.a_hat
    c_a_before = before.c_a
    count_before = before.a_samples_count

    # measurement = (0.8 + 0.8) / 0.8 = 2.0 — robust_z ≈ 101 >> 4.5
    outlier = estimator.update_a(ASample(dTdt=0.8, delta_out=8.0, setpoint_error=1.5, u_eff=0.8))

    assert outlier.a_updated is False
    assert outlier.updated is False
    assert outlier.a_last_reason == "a_measurement_outlier_mad"
    assert outlier.a_hat == pytest.approx(a_hat_before)
    assert outlier.c_a == pytest.approx(c_a_before)
    assert outlier.a_samples_count == count_before


def test_mad_filter_disabled_with_small_history() -> None:
    """With fewer than MAD_OUTLIER_MIN_SAMPLES, any physically valid b sample is accepted."""
    estimator = ParameterEstimator()

    # Feed MAD_OUTLIER_MIN_SAMPLES - 1 stable samples
    for dTdt in _STABLE_B_DTDTS[: MAD_OUTLIER_MIN_SAMPLES - 1]:
        estimator.update_b(BSample(dTdt=dTdt, delta_out=8.0, setpoint_error=1.5, u_eff=0.0))

    assert estimator._b_estimator.samples_count == MAD_OUTLIER_MIN_SAMPLES - 1

    # A far but physically valid measurement must not be rejected
    update = estimator.update_b(BSample(dTdt=-3.2, delta_out=8.0, setpoint_error=1.5, u_eff=0.0))

    assert update.b_updated is True
    assert update.b_last_reason == "sample_accepted"
    assert update.b_samples_count == MAD_OUTLIER_MIN_SAMPLES


def test_persistent_b_outlier_regime_is_confirmed() -> None:
    """Three coherent b outliers replace the estimator baseline on the third candidate."""
    estimator = ParameterEstimator()
    _build_stable_b(estimator)
    b_hat_before = estimator.b_hat

    # Coherent outlier measurements: 0.39, 0.40, 0.41 — all >> baseline 0.10
    first = estimator.update_b(BSample(dTdt=-3.12, delta_out=8.0, setpoint_error=1.5, u_eff=0.0))
    assert first.b_updated is False
    assert first.b_last_reason == "b_measurement_outlier_mad"

    second = estimator.update_b(BSample(dTdt=-3.20, delta_out=8.0, setpoint_error=1.5, u_eff=0.0))
    assert second.b_updated is False
    assert second.b_last_reason == "b_measurement_outlier_mad"

    third = estimator.update_b(BSample(dTdt=-3.28, delta_out=8.0, setpoint_error=1.5, u_eff=0.0))
    assert third.b_updated is True
    assert third.b_last_reason == "b_outlier_regime_confirmed"
    assert third.b_samples_count == OUTLIER_REGIME_CONFIRMATION_COUNT
    assert third.b_hat == pytest.approx(0.40, abs=0.02)
    assert abs(third.b_hat - b_hat_before) > 0.20


def test_persistent_a_outlier_regime_is_confirmed() -> None:
    """Three coherent a outliers replace the estimator baseline on the third candidate."""
    estimator = ParameterEstimator()
    _build_stable_b(estimator)
    _build_stable_a(estimator)
    a_hat_before = estimator.a_hat

    # Coherent a outlier measurements: 1.8, 1.9, 2.0 — all >> baseline 0.50
    # measurement = (dTdt + 0.80) / 0.8; for 1.8: dTdt=0.64, 1.9: 0.72, 2.0: 0.80
    first = estimator.update_a(ASample(dTdt=0.64, delta_out=8.0, setpoint_error=1.5, u_eff=0.8))
    assert first.a_updated is False
    assert first.a_last_reason == "a_measurement_outlier_mad"

    second = estimator.update_a(ASample(dTdt=0.72, delta_out=8.0, setpoint_error=1.5, u_eff=0.8))
    assert second.a_updated is False
    assert second.a_last_reason == "a_measurement_outlier_mad"

    third = estimator.update_a(ASample(dTdt=0.80, delta_out=8.0, setpoint_error=1.5, u_eff=0.8))
    assert third.a_updated is True
    assert third.a_last_reason == "a_outlier_regime_confirmed"
    assert third.a_samples_count == OUTLIER_REGIME_CONFIRMATION_COUNT
    assert third.a_hat == pytest.approx(1.9, abs=0.1)
    assert abs(third.a_hat - a_hat_before) > 1.0


def test_normal_b_sample_clears_outlier_candidate_evidence() -> None:
    """A normal sample after an outlier candidate resets the candidate buffer."""
    estimator = ParameterEstimator()
    _build_stable_b(estimator)

    # First outlier — 1 candidate, rejected
    first = estimator.update_b(BSample(dTdt=-3.2, delta_out=8.0, setpoint_error=1.5, u_eff=0.0))
    assert first.b_updated is False
    assert first.b_last_reason == "b_measurement_outlier_mad"

    # Normal sample — accepted, candidates cleared
    normal = estimator.update_b(BSample(dTdt=-0.80, delta_out=8.0, setpoint_error=1.5, u_eff=0.0))
    assert normal.b_updated is True
    assert normal.b_last_reason == "sample_accepted"

    # Two more outliers — candidate count restarts from zero, so still only 2 candidates
    second = estimator.update_b(BSample(dTdt=-3.12, delta_out=8.0, setpoint_error=1.5, u_eff=0.0))
    assert second.b_updated is False
    assert second.b_last_reason == "b_measurement_outlier_mad"

    third = estimator.update_b(BSample(dTdt=-3.20, delta_out=8.0, setpoint_error=1.5, u_eff=0.0))
    assert third.b_updated is False
    assert third.b_last_reason == "b_measurement_outlier_mad"


def test_incoherent_b_outlier_candidate_blocks_regime_confirmation() -> None:
    """A contradictory candidate among the last N prevents regime confirmation.

    Scenario: two high outliers (~0.40) then one low outlier (~0.01).
    The candidate deque becomes [0.40, 0.40, 0.01].  median(abs deviations)
    would be 0.0 (a bug), but max(abs deviations)/center = 0.975 >> 0.20,
    so the regime must NOT be confirmed.
    """
    estimator = ParameterEstimator()
    _build_stable_b(estimator)  # 5 samples around 0.10, MAD=0.01

    # Two high outliers: measurement 0.40 — both classified as outliers, both rejected
    first = estimator.update_b(BSample(dTdt=-3.20, delta_out=8.0, setpoint_error=1.5, u_eff=0.0))
    assert first.b_updated is False
    assert first.b_last_reason == "b_measurement_outlier_mad"

    second = estimator.update_b(BSample(dTdt=-3.20, delta_out=8.0, setpoint_error=1.5, u_eff=0.0))
    assert second.b_updated is False
    assert second.b_last_reason == "b_measurement_outlier_mad"

    # Contradictory low outlier: measurement 0.01 — also an outlier (robust_z ≈ 6 > 4.5),
    # but incoherent with the previous two; candidates = [0.40, 0.40, 0.01].
    third = estimator.update_b(BSample(dTdt=-0.08, delta_out=8.0, setpoint_error=1.5, u_eff=0.0))
    assert third.b_updated is False
    assert third.b_last_reason == "b_measurement_outlier_mad"


# ---------------------------------------------------------------------------
# Adaptive window policy — flexible A/B learning tests
# ---------------------------------------------------------------------------


def _make_off_entries(
    tins: list[float],
    *,
    tout: float = 5.0,
    target: float = 20.0,
    cycle_duration_min: float = 5.0,
    is_estimator_informative: bool = True,
) -> tuple[CycleHistoryEntry, ...]:
    """Build a sequence of OFF entries followed by a valid terminal entry."""
    entries = []
    for tin in tins[:-1]:
        entries.append(
            CycleHistoryEntry(
                tin=tin,
                tout=tout,
                target_temp=target,
                applied_power=0.0,
                is_valid=True,
                is_informative=True,
                is_estimator_informative=is_estimator_informative,
                cycle_duration_min=cycle_duration_min,
            )
        )
    entries.append(
        CycleHistoryEntry(
            tin=tins[-1],
            tout=tout,
            target_temp=target,
            applied_power=0.0,
            is_valid=True,
            is_informative=False,
            is_estimator_informative=False,
            cycle_duration_min=cycle_duration_min,
        )
    )
    return tuple(entries)


def test_learning_window_extends_beyond_three_cycles_for_short_cycles() -> None:
    """Short 5-min cycles must be allowed to accumulate beyond the old 3-cycle cap."""
    # 6 OFF cycles of 5 min; cooling is too slow to exceed 0.08 in 3 cycles
    # but accumulates enough amplitude over 5 cycles (total 25 min).
    tins = [20.0, 19.98, 19.96, 19.94, 19.92, 19.88, 19.80]
    observations = _make_off_entries(tins, cycle_duration_min=5.0)

    result = build_learning_window(
        observations,
        nd_hat=0.0,
        regime=WINDOW_REGIME_OFF,
    )

    assert result.sample is not None, result.reason
    assert result.sample.cycle_count > 3
    assert result.sample.dTdt < 0
    assert result.sample.total_duration_min > 15.0
    # dTdt must remain per-cycle, not per-minute
    assert abs(result.sample.dTdt) < 0.5


def test_learning_window_relaxed_amplitude_accepted_with_directional_steps() -> None:
    """An OFF window with amplitude between 0.05 and 0.08 and consistent steps is accepted."""
    # 3 OFF cycles, each dropping ~0.02 °C → amplitude = 0.06, 3 directional steps
    tins = [20.00, 19.98, 19.96, 19.94]
    observations = _make_off_entries(tins, cycle_duration_min=4.0)

    result = build_learning_window(
        observations,
        nd_hat=0.0,
        regime=WINDOW_REGIME_OFF,
    )

    assert result.sample is not None, result.reason
    assert result.sample.amplitude == pytest.approx(-0.06)
    assert result.sample.dTdt < 0


def test_learning_window_relaxed_amplitude_rejected_when_alternating() -> None:
    """A window with amplitude in the relaxed band must not be accepted if steps alternate."""
    # Amplitude end-to-end = -0.06, but the intermediate step goes the wrong way.
    tins = [20.00, 19.96, 19.99, 19.94]
    observations = _make_off_entries(tins, cycle_duration_min=4.0)

    result = build_learning_window(
        observations,
        nd_hat=0.0,
        regime=WINDOW_REGIME_OFF,
    )

    # Net amplitude ≈ -0.06 (relaxed band), but not enough directional steps
    assert result.sample is None
    assert "waiting" in result.reason


def test_learning_window_sliding_start_off_skips_inertial_warmup() -> None:
    """OFF window must slide past an initial inertial warm-up and produce a b sample."""
    # HEAT mode: first 2 points still rising (inertia), then 3 points cooling.
    tins = [19.50, 19.55, 19.60, 19.45, 19.28, 19.10]
    observations = _make_off_entries(tins, cycle_duration_min=5.0)

    result = build_learning_window(
        observations,
        nd_hat=0.0,
        regime=WINDOW_REGIME_OFF,
        mode_sign=1,
    )

    assert result.sample is not None, result.reason
    assert result.sample.dTdt < 0
    assert result.sample.amplitude < 0


def test_learning_window_sliding_start_off_rejects_when_no_cooling_segment() -> None:
    """OFF window must not produce a sample when every segment keeps warming."""
    tins = [19.0, 19.1, 19.2, 19.3, 19.4]
    observations = _make_off_entries(tins, cycle_duration_min=5.0)

    result = build_learning_window(
        observations,
        nd_hat=0.0,
        regime=WINDOW_REGIME_OFF,
        mode_sign=1,
    )

    assert result.sample is None
    # Either no-thermal-loss (if at max cycles) or waiting-more-signal
    assert result.reason in ("off_window_no_thermal_loss", "off_window_waiting_more_signal")


def test_learning_window_off_near_setpoint_feeds_b() -> None:
    """An OFF window near setpoint must still feed b when the thermal signal is good."""
    # setpoint_error ≈ 0.05 (well below 0.2), but delta_out = 10.0 and signal is clear.
    tins = [20.05, 19.90, 19.70]
    observations = tuple(
        CycleHistoryEntry(
            tin=tin,
            tout=10.0,
            target_temp=20.0,
            applied_power=0.0,
            is_valid=True,
            is_informative=True,
            is_estimator_informative=True,
            cycle_duration_min=5.0,
        )
        for tin in tins
    )

    result = build_anchored_learning_window(
        observations,
        nd_hat=0.0,
        regime=WINDOW_REGIME_OFF,
        end_index=1,
        mode_sign=1,
    )

    assert result.sample is not None, result.reason
    assert result.sample.allow_near_setpoint_b is True
    assert result.sample.setpoint_error < 0.2


def test_estimator_b_accepts_sample_with_low_delta_out() -> None:
    """b must accept a sample with delta_out >= 0.5 (below the old 1.0 gate)."""
    estimator = ParameterEstimator()

    update = estimator.update_b(
        BSample(
            dTdt=-0.04,
            delta_out=0.7,
            setpoint_error=1.0,
            u_eff=0.0,
        )
    )

    assert update.b_updated is True
    assert update.b_hat > 0.0
    assert update.b_last_reason == "sample_accepted"


def test_estimator_b_rejects_sample_with_very_low_delta_out() -> None:
    """b must reject a sample with delta_out < 0.5."""
    estimator = ParameterEstimator()

    update = estimator.update_b(
        BSample(
            dTdt=-0.04,
            delta_out=0.4,
            setpoint_error=1.0,
            u_eff=0.0,
        )
    )

    assert update.b_updated is False
    assert update.b_last_reason == "b_delta_out_too_small"


def test_estimator_a_still_requires_full_delta_out() -> None:
    """a must still reject samples with delta_out < 1.0 despite the b relaxation."""
    estimator = ParameterEstimator()
    for _ in range(4):
        estimator.update_b(
            BSample(dTdt=-0.08, delta_out=8.0, setpoint_error=1.0, u_eff=0.0)
        )

    update = estimator.update_a(
        ASample(
            dTdt=0.06,
            delta_out=0.7,
            setpoint_error=1.0,
            u_eff=0.6,
        )
    )

    assert update.a_updated is False
    assert update.a_last_reason == "a_delta_out_too_small"


def test_learning_window_flat_amplitude_below_relaxed_threshold_never_accepted() -> None:
    """A very flat window must never produce a sample, regardless of window length."""
    # Amplitude << 0.05 across many cycles — thermal signal is absent.
    tins = [20.000, 19.998, 19.996, 19.994, 19.992, 19.990, 19.988]
    observations = _make_off_entries(tins, cycle_duration_min=5.0)

    result = build_learning_window(
        observations,
        nd_hat=0.0,
        regime=WINDOW_REGIME_OFF,
    )

    assert result.sample is None
    assert "waiting" in result.reason
