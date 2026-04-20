"""Lightweight cycle-aligned learning windows for Adaptive TPI."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, floor

from .deadtime import CycleHistoryEntry

WINDOW_REGIME_OFF = "off"
WINDOW_REGIME_ON = "on"
WINDOW_REGIME_MIXED = "mixed"

OFF_POWER_MAX = 0.10
ON_POWER_MIN = 0.25
MIN_WINDOW_AMPLITUDE = 0.08
MIN_WINDOW_DURATION_MIN = 8.0
MAX_WINDOW_CYCLES = 3
MAX_WINDOW_DURATION_MIN = 45.0
MAX_SETPOINT_JUMP = 0.3


@dataclass(slots=True)
class LearningWindowSample:
    """Aggregated observables produced by a short learning window."""

    regime: str
    dTdt: float
    u_eff: float
    delta_out: float
    setpoint_error: float
    cycle_count: int
    points_count: int
    total_duration_min: float
    amplitude: float


@dataclass(slots=True)
class LearningWindowResult:
    """Describe whether a cycle-aligned learning window is exploitable."""

    sample: LearningWindowSample | None
    reason: str
    waiting_next_cycle: bool
    deadtime_blackout_active: bool = False


def classify_cycle_regime(applied_power: float) -> str:
    """Classify one cycle into the coarse learning regimes."""
    if applied_power <= OFF_POWER_MAX:
        return WINDOW_REGIME_OFF
    if applied_power >= ON_POWER_MIN:
        return WINDOW_REGIME_ON
    return WINDOW_REGIME_MIXED


def build_learning_window(
    observations: tuple[CycleHistoryEntry, ...],
    *,
    nd_hat: float,
    regime: str,
) -> LearningWindowResult:
    """Build the latest short bounded learning window for one regime."""
    if len(observations) < 2:
        return LearningWindowResult(
            sample=None,
            reason="window_insufficient_history",
            waiting_next_cycle=False,
        )

    end_index = _find_latest_candidate_end(observations, regime=regime)
    if end_index is None:
        return LearningWindowResult(
            sample=None,
            reason=f"{regime}_window_no_candidate",
            waiting_next_cycle=False,
        )

    return build_anchored_learning_window(
        observations,
        nd_hat=nd_hat,
        regime=regime,
        end_index=end_index,
    )


def build_anchored_learning_window(
    observations: tuple[CycleHistoryEntry, ...],
    *,
    nd_hat: float,
    regime: str,
    end_index: int,
) -> LearningWindowResult:
    """Build a bounded learning window anchored on one chosen cycle."""
    if len(observations) < 2:
        return LearningWindowResult(
            sample=None,
            reason="window_insufficient_history",
            waiting_next_cycle=False,
        )
    if end_index < 0 or end_index >= len(observations) - 1:
        return LearningWindowResult(
            sample=None,
            reason=f"{regime}_window_invalid_anchor",
            waiting_next_cycle=False,
        )
    if not observations[end_index].is_estimator_informative:
        return LearningWindowResult(
            sample=None,
            reason=f"{regime}_window_not_estimator_informative",
            waiting_next_cycle=False,
        )
    if not _matches_regime(observations[end_index], regime):
        return LearningWindowResult(
            sample=None,
            reason=f"{regime}_window_anchor_regime_mismatch",
            waiting_next_cycle=False,
        )
    if not observations[end_index + 1].is_valid:
        return LearningWindowResult(
            sample=None,
            reason=f"{regime}_window_missing_terminal_point",
            waiting_next_cycle=False,
        )

    start_index = end_index
    total_duration_min = _duration_minutes(observations[end_index])
    cycle_count = 1
    while start_index > 0 and cycle_count < MAX_WINDOW_CYCLES:
        previous = observations[start_index - 1]
        if not previous.is_estimator_informative:
            break
        previous_regime = classify_cycle_regime(previous.applied_power)
        if previous_regime != regime and previous_regime != WINDOW_REGIME_MIXED:
            break
        next_duration = total_duration_min + _duration_minutes(previous)
        if next_duration > MAX_WINDOW_DURATION_MIN:
            break
        start_index -= 1
        total_duration_min = next_duration
        cycle_count += 1
        if previous_regime == WINDOW_REGIME_MIXED:
            break

    safe_start_index = _safe_window_start_after_recent_setpoint_jump(
        observations,
        start_index=start_index,
        end_index=end_index,
        guard_cycles=_setpoint_guard_cycles(nd_hat),
        regime=regime,
    )
    if safe_start_index is None:
        return LearningWindowResult(
            sample=None,
            reason=f"{regime}_window_setpoint_changed",
            waiting_next_cycle=False,
        )
    if safe_start_index != start_index:
        start_index = safe_start_index
        cycle_slice = observations[start_index : end_index + 1]
        cycle_count = len(cycle_slice)
        total_duration_min = sum(_duration_minutes(entry) for entry in cycle_slice)

    deadtime_safe_start_index, blackout_active = _safe_window_start_after_recent_regime_transition(
        observations,
        start_index=start_index,
        end_index=end_index,
        regime=regime,
        guard_cycles=_deadtime_guard_cycles(nd_hat),
    )
    if deadtime_safe_start_index is None:
        return LearningWindowResult(
            sample=None,
            reason=f"{regime}_window_deadtime_blackout",
            waiting_next_cycle=False,
            deadtime_blackout_active=blackout_active,
        )
    if deadtime_safe_start_index != start_index:
        start_index = deadtime_safe_start_index
        cycle_slice = observations[start_index : end_index + 1]
        cycle_count = len(cycle_slice)
        total_duration_min = sum(_duration_minutes(entry) for entry in cycle_slice)

    end_observation = observations[end_index + 1]

    start_observation = observations[start_index]
    amplitude = end_observation.tin - start_observation.tin
    if regime == WINDOW_REGIME_OFF and amplitude >= 0.0:
        return LearningWindowResult(
            sample=None,
            reason="off_window_external_gain",
            waiting_next_cycle=False,
        )
    if regime == WINDOW_REGIME_ON and amplitude <= 0.0:
        return LearningWindowResult(
            sample=None,
            reason="on_window_no_heating_effect",
            waiting_next_cycle=False,
        )
    if (
        abs(amplitude) < MIN_WINDOW_AMPLITUDE
        or total_duration_min < MIN_WINDOW_DURATION_MIN
    ):
        return LearningWindowResult(
            sample=None,
            reason=f"{regime}_window_waiting_more_signal",
            waiting_next_cycle=(cycle_count < MAX_WINDOW_CYCLES),
        )

    delayed_powers: list[float] = []
    cycle_slice = observations[start_index : end_index + 1]
    for index in range(start_index, end_index + 1):
        delayed_index = index - floor(max(nd_hat, 0.0))
        if delayed_index < 0:
            continue
        delayed_powers.append(observations[delayed_index].applied_power)

    if regime == WINDOW_REGIME_ON and not delayed_powers:
        return LearningWindowResult(
            sample=None,
            reason="on_window_deadtime_alignment_missing",
            waiting_next_cycle=False,
        )

    u_eff = (
        sum(delayed_powers) / len(delayed_powers)
        if delayed_powers
        else sum(entry.applied_power for entry in cycle_slice) / len(cycle_slice)
    )
    delta_out = sum(entry.tin - entry.tout for entry in cycle_slice) / len(cycle_slice)
    setpoint_error = sum(abs(entry.target_temp - entry.tin) for entry in cycle_slice) / len(cycle_slice)

    return LearningWindowResult(
        sample=LearningWindowSample(
            regime=regime,
            # The adaptive model is cycle-discrete, so learning samples must keep
            # a temperature delta per cycle rather than per minute.
            dTdt=amplitude / max(1, cycle_count),
            u_eff=u_eff,
            delta_out=delta_out,
            setpoint_error=setpoint_error,
            cycle_count=cycle_count,
            points_count=cycle_count + 1,
            total_duration_min=total_duration_min,
            amplitude=amplitude,
        ),
        reason=f"{regime}_window_ready",
        waiting_next_cycle=False,
        deadtime_blackout_active=blackout_active,
    )


def _find_latest_candidate_end(
    observations: tuple[CycleHistoryEntry, ...],
    *,
    regime: str,
) -> int | None:
    for candidate_index in range(len(observations) - 2, -1, -1):
        current = observations[candidate_index]
        nxt = observations[candidate_index + 1]
        if not current.is_estimator_informative or not nxt.is_valid:
            continue
        if _matches_regime(current, regime):
            return candidate_index
    return None


def _matches_regime(entry: CycleHistoryEntry, regime: str) -> bool:
    return classify_cycle_regime(entry.applied_power) == regime


def _duration_minutes(entry: CycleHistoryEntry) -> float:
    return max(0.0, float(entry.cycle_duration_min))


def _has_setpoint_jump(left: CycleHistoryEntry, right: CycleHistoryEntry) -> bool:
    return abs(left.target_temp - right.target_temp) > MAX_SETPOINT_JUMP


def _blocks_regime_for_setpoint(
    left: CycleHistoryEntry,
    right: CycleHistoryEntry,
    *,
    regime: str,
) -> bool:
    """Return True when a setpoint jump contradicts the active learning regime."""
    if not _has_setpoint_jump(left, right):
        return False
    if regime == WINDOW_REGIME_ON:
        return right.target_temp < left.target_temp
    if regime == WINDOW_REGIME_OFF:
        return right.target_temp > left.target_temp
    return True


def _setpoint_guard_cycles(nd_hat: float) -> int:
    """Return the post-setpoint-change learning blackout in cycles."""
    return max(1, int(ceil(max(nd_hat, 0.0))))


def _deadtime_guard_cycles(nd_hat: float) -> int:
    """Return the learning blackout after a regime transition."""
    return max(1, int(ceil(max(nd_hat, 0.0))))


def _safe_window_start_after_recent_setpoint_jump(
    observations: tuple[CycleHistoryEntry, ...],
    *,
    start_index: int,
    end_index: int,
    guard_cycles: int,
    regime: str,
) -> int | None:
    """Return the earliest safe start index after the latest setpoint jump."""
    latest_jump_following_index: int | None = None
    for index in range(0, end_index + 1):
        if _blocks_regime_for_setpoint(
            observations[index],
            observations[index + 1],
            regime=regime,
        ):
            latest_jump_following_index = index + 1

    if latest_jump_following_index is None:
        return start_index

    safe_start_index = latest_jump_following_index + guard_cycles
    if start_index >= safe_start_index:
        return start_index
    if safe_start_index > end_index:
        return None
    return safe_start_index


def _safe_window_start_after_recent_regime_transition(
    observations: tuple[CycleHistoryEntry, ...],
    *,
    start_index: int,
    end_index: int,
    regime: str,
    guard_cycles: int,
) -> tuple[int | None, bool]:
    """Return the earliest safe index after the latest effective ON/OFF transition."""
    latest_target_index: int | None = None
    latest_opposite_index: int | None = None

    for index in range(end_index, -1, -1):
        current_regime = classify_cycle_regime(observations[index].applied_power)
        if current_regime == WINDOW_REGIME_MIXED:
            continue
        if current_regime == regime:
            if latest_target_index is None:
                latest_target_index = index
            continue
        latest_opposite_index = index
        break

    if latest_target_index is None or latest_opposite_index is None:
        return start_index, False

    latest_transition_following_index = latest_opposite_index + 1
    safe_start_index = latest_transition_following_index + guard_cycles
    if start_index >= safe_start_index:
        return start_index, False
    if safe_start_index > end_index:
        return None, True
    return safe_start_index, True
