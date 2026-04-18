"""Behavioral tests for the Adaptive TPI implementation roadmap."""

from __future__ import annotations

import pytest

from custom_components.vtherm_adaptive_tpi.algo import AdaptiveTPIAlgorithm
from custom_components.vtherm_adaptive_tpi.adaptive_tpi.controller import (
    compute_gain_targets,
    project_gains,
)
from custom_components.vtherm_adaptive_tpi.adaptive_tpi.deadtime import (
    DeadtimeModel,
    DeadtimeObservation,
)
from custom_components.vtherm_adaptive_tpi.adaptive_tpi.estimator import (
    A_MAX,
    A_MIN,
    B_MAX,
    B_MIN,
    EstimatorSample,
    ParameterEstimator,
)
from custom_components.vtherm_adaptive_tpi.adaptive_tpi.supervisor import (
    PHASE_A,
    PHASE_C,
)


def _make_deadtime_lock_sequence() -> list[DeadtimeObservation]:
    """Build a synthetic sequence whose best coarse deadtime is one cycle."""
    observations: list[DeadtimeObservation] = []
    tin = 19.0
    tout = 10.0
    target = 21.0
    powers = [
        0.0,
        0.0,
        0.7,
        0.7,
        0.7,
        0.2,
        0.0,
        0.0,
        0.8,
        0.8,
        0.3,
        0.0,
        0.0,
        0.9,
        0.9,
        0.4,
        0.0,
        0.0,
        0.75,
        0.75,
    ]

    for index, power in enumerate(powers):
        observations.append(
            DeadtimeObservation(
                tin=tin,
                tout=tout,
                target_temp=target,
                applied_power=power,
            )
        )
        delayed_power = powers[index - 1] if index >= 1 else 0.0
        tin += (0.4 * delayed_power) - (0.05 * (tin - tout))

    return observations


def test_startup_with_no_history_keeps_bootstrap_defaults() -> None:
    """Fresh runtime should expose deterministic startup diagnostics."""
    algo = AdaptiveTPIAlgorithm(name="test-startup")

    diagnostics = algo.get_diagnostics()

    assert diagnostics["bootstrap_phase"] == "startup"
    assert diagnostics["accepted_cycles_count"] == 0
    assert diagnostics["Kint"] == pytest.approx(0.6)
    assert diagnostics["Kext"] == pytest.approx(0.01)


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
    assert algo.on_percent is None
    assert diagnostics["last_freeze_reason"] == "missing_temperature"
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
    assert algo.on_percent == pytest.approx(0.0)
    assert diagnostics["last_freeze_reason"] == "hvac_mode_incompatible"
    assert diagnostics["debug"]["last_cycle_classification"] == "rejected"


def test_deadtime_lock_success_after_consistent_accepted_cycles() -> None:
    """The coarse deadtime search should lock on a dominant one-cycle delay."""
    model = DeadtimeModel()

    for observation in _make_deadtime_lock_sequence():
        result = model.record_accepted_observation(observation)

    assert result.locked is True
    assert result.lock_reason is None
    assert result.nd_hat == pytest.approx(1.0)
    assert result.best_candidate == pytest.approx(1.0)
    assert result.c_nd >= 0.6


def test_deadtime_lock_failure_stays_explicit_before_enough_cycles() -> None:
    """The deadtime search should report why it is not yet lockable."""
    model = DeadtimeModel()

    for observation in _make_deadtime_lock_sequence()[:9]:
        result = model.record_accepted_observation(observation)

    assert result.locked is False
    assert result.lock_reason == "deadtime_insufficient_cycles"
    assert result.c_nd < 0.6


def test_estimator_updates_stay_bounded_under_extreme_samples() -> None:
    """The constrained estimator should never leave its configured bounds."""
    estimator = ParameterEstimator()
    positive_sample = EstimatorSample(
        y=20.0,
        u_del=50.0,
        loss_input=50.0,
        c_nd=1.0,
        i_a=1.0,
        i_b=1.0,
        i_global=1.0,
    )
    negative_sample = EstimatorSample(
        y=-20.0,
        u_del=50.0,
        loss_input=50.0,
        c_nd=1.0,
        i_a=1.0,
        i_b=1.0,
        i_global=1.0,
    )

    for _ in range(50):
        update = estimator.update(positive_sample)
        assert update.updated is True

    for _ in range(50):
        update = estimator.update(negative_sample)
        assert A_MIN <= update.a_hat <= A_MAX
        assert B_MIN <= update.b_hat <= B_MAX
        assert 0.0 <= update.c_a <= 1.0
        assert 0.0 <= update.c_b <= 1.0


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

    diagnostics = algo.get_diagnostics()
    assert diagnostics["last_freeze_reason"] == "non_informative_cycle"
    assert diagnostics["debug"]["last_cycle_classification"] == "non_informative"
    assert diagnostics["accepted_cycles_count"] == 2


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

    diagnostics = algo.get_diagnostics()
    assert diagnostics["last_freeze_reason"] == "power_shedding"
    assert diagnostics["debug"]["last_cycle_classification"] == "rejected"
    assert diagnostics["accepted_cycles_count"] == 0


def test_gain_projection_matches_structural_formulas() -> None:
    """Gain targets and bounded projection should follow the math spec."""
    k_int_target, k_ext_target = compute_gain_targets(
        a_hat=0.2,
        b_hat=0.03,
        nd_hat=2.0,
    )

    assert k_int_target == pytest.approx(0.3125)
    assert k_ext_target == pytest.approx(0.15)

    next_k_int, next_k_ext = project_gains(
        phase=PHASE_C,
        k_int=0.2,
        k_ext=0.01,
        a_hat=0.2,
        b_hat=0.03,
        nd_hat=2.0,
    )

    assert next_k_int == pytest.approx(0.23)
    assert next_k_ext == pytest.approx(0.015)


def test_cycle_min_change_invalidates_persisted_warm_start() -> None:
    """A cycle duration change should rescale estimates and re-enter Phase A."""
    algo = AdaptiveTPIAlgorithm(name="test-persistence", debug_mode=True)
    algo._state.deadtime_locked = True
    algo._state.deadtime_candidate_costs = {"1": 0.1}
    algo._state.deadtime_best_candidate = 1.0

    algo.load_state(
        {
            "k_int": 0.6,
            "k_ext": 0.01,
            "nd_hat": 1.0,
            "a_hat": 0.1,
            "b_hat": 0.02,
            "bootstrap_phase": "phase_d",
        },
        current_cycle_min=10.0,
        persisted_cycle_min=5.0,
    )

    diagnostics = algo.get_diagnostics()
    assert diagnostics["bootstrap_phase"] == PHASE_A
    assert diagnostics["a_hat"] == pytest.approx(0.2)
    assert diagnostics["b_hat"] == pytest.approx(0.04)
    assert diagnostics["last_freeze_reason"] == "cycle_min_changed_revalidation"
    assert diagnostics["debug"]["deadtime_locked"] is False
    assert diagnostics["deadtime_candidate_costs"] == {}
