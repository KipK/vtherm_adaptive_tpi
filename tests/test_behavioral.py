"""Behavioral tests for the Adaptive TPI implementation roadmap."""

from __future__ import annotations

import math

import pytest

from custom_components.vtherm_adaptive_tpi.algo import AdaptiveTPIAlgorithm
from custom_components.vtherm_adaptive_tpi.adaptive_tpi.controller import (
    compute_gain_targets,
    project_gains,
)
from custom_components.vtherm_adaptive_tpi.adaptive_tpi.deadtime import (
    CONFIDENCE_LOCK_THRESHOLD,
    CycleHistoryEntry,
    DeadtimeModel,
    DeadtimeObservation,
    DeadtimeSearchResult,
    StepIdentification,
    _compute_b_proxy,
    _compute_weighted_moments,
    _collect_response,
    _find_latest_step,
)
from custom_components.vtherm_adaptive_tpi.adaptive_tpi.estimator import (
    ASample,
    A_MAX,
    A_MIN,
    BSample,
    B_MAX,
    B_MIN,
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


def test_startup_with_no_history_keeps_bootstrap_defaults() -> None:
    """Fresh runtime should expose deterministic startup diagnostics."""
    algo = AdaptiveTPIAlgorithm(name="test-startup")

    diagnostics = algo.get_diagnostics()

    assert diagnostics["bootstrap_phase"] == "startup"
    assert diagnostics["accepted_cycles_count"] == 0
    assert diagnostics["k_int"] == pytest.approx(0.6)
    assert diagnostics["k_ext"] == pytest.approx(0.01)
    assert diagnostics["deadtime_min"] is None
    assert diagnostics["a_hat_per_hour"] is None
    assert diagnostics["b_hat_per_hour"] is None
    assert diagnostics["tau_h"] is None
    assert diagnostics["tau_min"] is None


def test_diagnostics_expose_normalized_units_when_cycle_duration_is_known() -> None:
    """Diagnostics should expose user-facing normalized units in addition to per-cycle values."""
    algo = AdaptiveTPIAlgorithm(name="test-diag-units")
    algo._state.nd_hat = 2.0
    algo._state.a_hat = 0.2
    algo._state.b_hat = 0.03
    algo._state.cycle_min_at_last_accepted_cycle = 5.0

    diagnostics = algo.get_diagnostics()

    assert diagnostics["nd_hat"] == pytest.approx(2.0)
    assert diagnostics["nd_hat_cycles"] == pytest.approx(2.0)
    assert diagnostics["deadtime_min"] == pytest.approx(10.0)
    assert diagnostics["a_hat_per_hour"] == pytest.approx(2.4)
    assert diagnostics["b_hat_per_hour"] == pytest.approx(0.36)
    assert diagnostics["tau_h"] == pytest.approx(1.0 / 0.36)
    assert diagnostics["tau_min"] == pytest.approx((1.0 / 0.36) * 60.0)


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


def test_step_detection_finds_clean_on_transition() -> None:
    """A clean OFF→ON transition after stable OFF cycles should be detected."""
    observations = _make_step_observations(nd=2, n_off=4, n_on=1)
    step_index = _find_latest_step(observations, last_processed_step_index=-1)
    assert step_index == 4


def test_step_detection_ignores_insufficient_off_period() -> None:
    """A step preceded by fewer than N_OFF_MIN OFF cycles must not be detected."""
    observations = _make_step_observations(nd=2, n_off=2, n_on=1)
    step_index = _find_latest_step(observations, last_processed_step_index=-1)
    assert step_index is None


def test_step_collection_ends_on_plateau() -> None:
    """Collection must return the response list when a plateau is detected."""
    observations = _make_step_observations(nd=2, n_off=4)
    result = _collect_response(observations, step_index=4)
    assert isinstance(result, list)
    assert len(result) >= 4


def test_step_collection_aborts_on_power_cut() -> None:
    """An early power cut during collection must abort the identification."""
    base = list(_make_step_observations(nd=2, n_off=4, n_on=5))
    # Force power drop at index 7 (3 cycles into ON phase)
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
    result = _collect_response(tuple(base), step_index=4)
    assert result == "aborted"


def test_weighted_moments_returns_finite_nd() -> None:
    """A valid FOPDT step response must yield a positive finite nd estimate."""
    observations = _make_step_observations(nd=2, n_off=4)
    result = _collect_response(observations, step_index=4)
    assert isinstance(result, list)
    moments = _compute_weighted_moments(result)
    assert moments is not None
    nd_cycles, quality = moments
    assert math.isfinite(nd_cycles)
    assert nd_cycles >= 0.0
    assert 0.0 < quality <= 1.0


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
            StepIdentification(nd_cycles=2.0 + 0.05 * i, quality=0.85, b_proxy=0.03, cycle_index=i * 30)
        )
    result = model._recompute_nd_hat()
    assert result.locked is True
    assert result.lock_reason is None
    assert result.nd_hat == pytest.approx(2.05, abs=0.1)
    assert result.c_nd >= CONFIDENCE_LOCK_THRESHOLD
    assert result.best_candidate_b == pytest.approx(0.03)


def test_deadtime_model_no_lock_before_enough_identifications() -> None:
    """With fewer than N_LOCK_MIN identifications, lock_reason must be set."""
    model = DeadtimeModel()
    model._identifications.append(
        StepIdentification(nd_cycles=2.0, quality=0.9, b_proxy=None, cycle_index=0)
    )
    result = model._recompute_nd_hat()
    assert result.locked is False
    assert result.lock_reason == "deadtime_insufficient_identifications"
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
    assert result.lock_reason in ("deadtime_insufficient_separation", "deadtime_confidence_low")


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
        StepIdentification(nd_cycles=2.3, quality=0.75, b_proxy=0.04, cycle_index=10)
    )
    model._last_processed_step_index = 10
    model._recompute_nd_hat()

    saved = model.to_persisted_dict()

    model2 = DeadtimeModel()
    model2.load_persisted_dict(saved)

    assert len(model2._identifications) == 1
    assert model2._identifications[0].nd_cycles == pytest.approx(2.3)
    assert model2._identifications[0].quality == pytest.approx(0.75)
    assert model2._identifications[0].b_proxy == pytest.approx(0.04)
    assert model2._last_processed_step_index == 10
    assert model2.nd_hat == pytest.approx(2.3)


def test_deadtime_old_format_silently_ignored() -> None:
    """A persisted dict with the old cycle_history key must be silently discarded."""
    old_format = {
        "cycle_history": [{"tin": 19.0, "tout": 10.0, "target_temp": 21.0, "applied_power": 0.7}],
        "best_candidate_history": [1, 1, 1],
    }
    model = DeadtimeModel()
    model.load_persisted_dict(old_format)
    assert len(model._identifications) == 0
    assert model.nd_hat == pytest.approx(0.0)
    assert model.locked is False


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

    assert update.b_hat == pytest.approx(0.03)
    assert update.b_samples_count == 1
    assert update.b_last_reason == "b_seeded_from_deadtime"
    assert update.i_b == pytest.approx(0.0)



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


def test_learning_window_waits_for_more_signal_after_truncating_setpoint_jump_window() -> None:
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


def test_learning_window_allows_one_safe_cycle_after_setpoint_jump_without_deadtime() -> None:
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


def test_learning_window_does_not_restart_blackout_on_mixed_gap_inside_same_regime() -> None:
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
    assert result.reason == "off_window_external_gain"


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
    assert result.reason == "on_window_no_heating_effect"


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
    assert diagnostics["last_freeze_reason"] == "non_informative_cycle"
    assert diagnostics["debug"]["last_cycle_classification"] == "non_informative"
    assert diagnostics["accepted_cycles_count"] == 2


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
    assert diagnostics["current_cycle_regime"] == "on"
    assert diagnostics["learning_route_selected"] == "a"
    assert diagnostics["last_learning_attempt_regime"] == "a"
    assert diagnostics["a_samples_count"] >= 1


def test_deadtime_history_keeps_committed_cycle_power(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deadtime history must keep the cycle-start power captured for that cycle."""
    algo = AdaptiveTPIAlgorithm(name="test-deadtime-cycle-power", debug_mode=True)
    recorded_powers: list[float] = []

    def fake_record_cycle(observation, **kwargs):
        recorded_powers.append(observation.applied_power)
        return DeadtimeSearchResult(
            nd_hat=0.0,
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

    assert recorded_powers == [pytest.approx(0.8)]


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
    algo = AdaptiveTPIAlgorithm(name="test-deadtime-b-proxy")

    for i in range(3):
        algo._deadtime_model._identifications.append(
            StepIdentification(nd_cycles=2.0 + 0.05 * i, quality=0.85, b_proxy=0.03, cycle_index=i * 30)
        )

    result = algo._deadtime_model.evaluate()
    algo._state.deadtime_b_proxy = result.best_candidate_b
    algo._apply_estimator_update(algo._estimator.seed_b_from_deadtime_proxy(result.best_candidate_b))
    algo._refresh_b_crosscheck()

    diagnostics = algo.get_diagnostics()
    assert diagnostics["deadtime_b_proxy"] == pytest.approx(result.best_candidate_b)
    assert diagnostics["b_hat"] == pytest.approx(result.best_candidate_b)
    assert diagnostics["b_crosscheck_error"] == pytest.approx(0.0)
    assert diagnostics["b_methods_consistent"] is True


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
    assert diagnostics["k_int"] == pytest.approx(0.72)
    assert diagnostics["k_ext"] == pytest.approx(0.04)


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
    assert diagnostics["bootstrap_phase"] == "startup"
    assert diagnostics["k_int"] == pytest.approx(0.6)
    assert diagnostics["k_ext"] == pytest.approx(0.01)
    assert diagnostics["nd_hat"] == pytest.approx(0.0)
    assert diagnostics["c_nd"] == pytest.approx(0.0)
    assert diagnostics["a_hat"] == pytest.approx(0.001)
    assert diagnostics["b_hat"] == pytest.approx(0.0)
    assert diagnostics["accepted_cycles_count"] == 0
    assert diagnostics["debug"]["deadtime_locked"] is False


def test_cycle_min_change_invalidates_persisted_warm_start() -> None:
    """A cycle duration change should rescale estimates and re-enter Phase A."""
    algo = AdaptiveTPIAlgorithm(name="test-persistence", debug_mode=True)
    algo._state.deadtime_locked = True
    algo._state.deadtime_identification_qualities = {"1": 0.1}
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
    assert diagnostics["deadtime_identification_qualities"] == {}


def test_warm_start_restores_deadtime_model_and_candidate_costs() -> None:
    """A normal warm start should preserve the deadtime model, not only the summary state."""
    algo = AdaptiveTPIAlgorithm(name="test-deadtime-persistence", debug_mode=True)

    for i in range(3):
        algo._deadtime_model._identifications.append(
            StepIdentification(nd_cycles=2.0 + 0.05 * i, quality=0.85, b_proxy=0.04, cycle_index=i * 30)
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

    saved = algo.save_state()

    restored = AdaptiveTPIAlgorithm(name="test-deadtime-persistence-restore", debug_mode=True)
    restored.load_state(
        saved,
        current_cycle_min=5.0,
        persisted_cycle_min=5.0,
    )

    diagnostics = restored.get_diagnostics()
    assert diagnostics["nd_hat"] == pytest.approx(result.nd_hat)
    assert diagnostics["c_nd"] == pytest.approx(result.c_nd)
    assert diagnostics["deadtime_identification_qualities"] == result.candidate_costs
    assert diagnostics["deadtime_b_proxy"] == pytest.approx(result.best_candidate_b)
    assert diagnostics["debug"]["deadtime_best_candidate"] == pytest.approx(result.best_candidate)


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

    saved = algo.save_state()

    restored = AdaptiveTPIAlgorithm(name="test-estimator-persistence-restore", debug_mode=True)
    restored.load_state(
        saved,
        current_cycle_min=5.0,
        persisted_cycle_min=5.0,
    )

    diagnostics = restored.get_diagnostics()
    assert diagnostics["a_samples_count"] == len(a_samples)
    assert diagnostics["b_samples_count"] == len(b_samples)
    assert diagnostics["c_a"] == pytest.approx(algo._state.c_a)
    assert diagnostics["c_b"] == pytest.approx(algo._state.c_b)
    assert diagnostics["b_converged"] is True
    assert diagnostics["k_int"] != pytest.approx(0.6)
    assert diagnostics["k_ext"] != pytest.approx(0.01)


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
    assert diagnostics["bootstrap_phase"] == PHASE_A
    assert diagnostics["accepted_cycles_count"] == 10
    assert diagnostics["hours_without_excitation"] == pytest.approx(10 * 5.0 / 60.0)
    assert diagnostics["last_freeze_reason"] == "insufficient_excitation_bootstrap"
