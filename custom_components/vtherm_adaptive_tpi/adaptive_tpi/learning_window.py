"""Lightweight cycle-aligned learning windows for Adaptive TPI."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, floor

from .deadtime import CycleHistoryEntry
from .learning_policy import LearningWindowPolicy, build_learning_window_policy

WINDOW_REGIME_OFF = "off"
WINDOW_REGIME_ON = "on"
WINDOW_REGIME_MIXED = "mixed"

OFF_POWER_MAX = 0.10
ON_POWER_MIN = 0.25
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
    allow_near_setpoint_b: bool = False


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


def _can_feed_estimator(entry: CycleHistoryEntry, regime: str) -> bool:
    """Return True when one cycle may contribute to estimator learning."""
    if entry.is_estimator_informative:
        return True
    return regime == WINDOW_REGIME_OFF and entry.bootstrap_b_learning_allowed


def build_learning_window(
    observations: tuple[CycleHistoryEntry, ...],
    *,
    nd_hat: float,
    regime: str,
    mode_sign: int = 1,
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
        mode_sign=mode_sign,
    )


def build_anchored_learning_window(
    observations: tuple[CycleHistoryEntry, ...],
    *,
    nd_hat: float,
    regime: str,
    end_index: int,
    mode_sign: int = 1,
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
    if not _can_feed_estimator(observations[end_index], regime):
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

    cycle_duration_min = _duration_minutes(observations[end_index])
    policy = build_learning_window_policy(nd_hat=nd_hat, cycle_duration_min=cycle_duration_min)

    start_index = end_index
    total_duration_min = _duration_minutes(observations[end_index])
    cycle_count = 1
    while start_index > 0 and cycle_count < policy.max_cycles:
        previous = observations[start_index - 1]
        if not _can_feed_estimator(previous, regime):
            break
        previous_regime = classify_cycle_regime(previous.applied_demand)
        if previous_regime != regime and previous_regime != WINDOW_REGIME_MIXED:
            break
        next_duration = total_duration_min + _duration_minutes(previous)
        if next_duration > policy.max_duration_min:
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
        mode_sign=mode_sign,
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
    amplitude = end_observation.tin - observations[start_index].tin

    # Check thermal direction; try sliding start when the full window has the wrong sign.
    if regime == WINDOW_REGIME_OFF and mode_sign * amplitude >= 0.0:
        slid = _find_sliding_start(
            observations,
            start_index=start_index,
            end_index=end_index,
            regime=regime,
            mode_sign=mode_sign,
        )
        if slid is None:
            can_grow = (
                cycle_count < policy.max_cycles
                and total_duration_min < policy.max_duration_min
            )
            if can_grow:
                return LearningWindowResult(
                    sample=None,
                    reason="off_window_waiting_more_signal",
                    waiting_next_cycle=True,
                    deadtime_blackout_active=blackout_active,
                )
            return LearningWindowResult(
                sample=None,
                reason="off_window_no_thermal_loss",
                waiting_next_cycle=False,
                deadtime_blackout_active=blackout_active,
            )
        start_index = slid
        cycle_count = end_index - start_index + 1
        total_duration_min = sum(
            _duration_minutes(observations[i]) for i in range(start_index, end_index + 1)
        )
        amplitude = end_observation.tin - observations[start_index].tin

    elif regime == WINDOW_REGIME_ON and mode_sign * amplitude <= 0.0:
        slid = _find_sliding_start(
            observations,
            start_index=start_index,
            end_index=end_index,
            regime=regime,
            mode_sign=mode_sign,
        )
        if slid is None:
            can_grow = (
                cycle_count < policy.max_cycles
                and total_duration_min < policy.max_duration_min
            )
            if can_grow:
                return LearningWindowResult(
                    sample=None,
                    reason="on_window_waiting_more_signal",
                    waiting_next_cycle=True,
                    deadtime_blackout_active=blackout_active,
                )
            return LearningWindowResult(
                sample=None,
                reason="on_window_no_actuator_effect",
                waiting_next_cycle=False,
                deadtime_blackout_active=blackout_active,
            )
        start_index = slid
        cycle_count = end_index - start_index + 1
        total_duration_min = sum(
            _duration_minutes(observations[i]) for i in range(start_index, end_index + 1)
        )
        amplitude = end_observation.tin - observations[start_index].tin

    ready, reason, waiting = _thermal_signal_ready(
        observations=observations,
        start_index=start_index,
        end_index=end_index,
        regime=regime,
        mode_sign=mode_sign,
        amplitude=amplitude,
        total_duration_min=total_duration_min,
        policy=policy,
    )
    if not ready:
        can_grow = (
            cycle_count < policy.max_cycles
            and total_duration_min < policy.max_duration_min
        )
        return LearningWindowResult(
            sample=None,
            reason=reason,
            waiting_next_cycle=waiting and can_grow,
            deadtime_blackout_active=blackout_active,
        )

    delayed_powers: list[float] = []
    cycle_slice = observations[start_index : end_index + 1]
    for index in range(start_index, end_index + 1):
        delayed_index = index - floor(max(nd_hat, 0.0))
        if delayed_index < 0:
            continue
        delayed_powers.append(observations[delayed_index].applied_demand)

    if regime == WINDOW_REGIME_ON and not delayed_powers:
        return LearningWindowResult(
            sample=None,
            reason="on_window_deadtime_alignment_missing",
            waiting_next_cycle=False,
        )

    u_eff = (
        sum(delayed_powers) / len(delayed_powers)
        if delayed_powers
        else sum(entry.applied_demand for entry in cycle_slice) / len(cycle_slice)
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
            # All ready OFF windows allow b to learn near setpoint because
            # b = -dTdt/delta_out does not require a setpoint gap to be valid.
            allow_near_setpoint_b=(regime == WINDOW_REGIME_OFF) or any(
                entry.bootstrap_b_learning_allowed for entry in cycle_slice
            ),
        ),
        reason=f"{regime}_window_ready",
        waiting_next_cycle=False,
        deadtime_blackout_active=blackout_active,
    )


def _thermal_signal_ready(
    *,
    observations: tuple[CycleHistoryEntry, ...],
    start_index: int,
    end_index: int,
    regime: str,
    mode_sign: int,
    amplitude: float,
    total_duration_min: float,
    policy: LearningWindowPolicy,
) -> tuple[bool, str, bool]:
    """Decide whether the thermal signal is ready, relaxed-ready, or still waiting."""
    abs_amp = abs(amplitude)

    if abs_amp >= policy.min_amplitude and total_duration_min >= policy.min_duration_min:
        return True, f"{regime}_window_ready", False

    if abs_amp >= policy.relaxed_min_amplitude and total_duration_min >= policy.min_duration_min:
        steps = _count_directional_steps(
            observations,
            start_index=start_index,
            end_index=end_index,
            mode_sign=mode_sign,
            regime=regime,
        )
        if steps >= policy.min_directional_steps:
            return True, f"{regime}_window_ready", False

    return False, f"{regime}_window_waiting_more_signal", True


def _count_directional_steps(
    observations: tuple[CycleHistoryEntry, ...],
    *,
    start_index: int,
    end_index: int,
    mode_sign: int,
    regime: str,
) -> int:
    """Count temperature steps in the thermodynamically correct direction."""
    # OFF in HEAT: expect tin to decrease → effective_sign = -1 * 1 = -1
    # ON  in HEAT: expect tin to increase → effective_sign = +1 * 1 = +1
    # OFF in COOL: expect tin to increase → effective_sign = -1 * -1 = +1
    # ON  in COOL: expect tin to decrease → effective_sign = +1 * -1 = -1
    regime_sign = -1 if regime == WINDOW_REGIME_OFF else 1
    effective_sign = regime_sign * mode_sign
    count = 0
    for i in range(start_index, end_index + 1):
        delta = observations[i + 1].tin - observations[i].tin
        if effective_sign * delta > 0:
            count += 1
    return count


def _find_sliding_start(
    observations: tuple[CycleHistoryEntry, ...],
    *,
    start_index: int,
    end_index: int,
    regime: str,
    mode_sign: int,
) -> int | None:
    """Find the earliest start index where the window has the correct thermal sign.

    Slides forward from start_index + 1 only — never before the guard-imposed boundary.
    """
    end_tin = observations[end_index + 1].tin
    for candidate in range(start_index + 1, end_index + 1):
        candidate_amplitude = end_tin - observations[candidate].tin
        if regime == WINDOW_REGIME_OFF and mode_sign * candidate_amplitude < 0:
            return candidate
        if regime == WINDOW_REGIME_ON and mode_sign * candidate_amplitude > 0:
            return candidate
    return None


def _find_latest_candidate_end(
    observations: tuple[CycleHistoryEntry, ...],
    *,
    regime: str,
) -> int | None:
    for candidate_index in range(len(observations) - 2, -1, -1):
        current = observations[candidate_index]
        nxt = observations[candidate_index + 1]
        if not _can_feed_estimator(current, regime) or not nxt.is_valid:
            continue
        if _matches_regime(current, regime):
            return candidate_index
    return None


def _matches_regime(entry: CycleHistoryEntry, regime: str) -> bool:
    return classify_cycle_regime(entry.applied_demand) == regime


def _duration_minutes(entry: CycleHistoryEntry) -> float:
    return max(0.0, float(entry.cycle_duration_min))


def _has_setpoint_jump(left: CycleHistoryEntry, right: CycleHistoryEntry) -> bool:
    return abs(left.target_temp - right.target_temp) > MAX_SETPOINT_JUMP


def _blocks_regime_for_setpoint(
    left: CycleHistoryEntry,
    right: CycleHistoryEntry,
    *,
    regime: str,
    mode_sign: int = 1,
) -> bool:
    """Return True when a setpoint jump contradicts the active learning regime."""
    if not _has_setpoint_jump(left, right):
        return False
    delta_sp = right.target_temp - left.target_temp
    # A jump is contradictory when it opposes the current regime direction.
    # HEAT: ON tolerates upward jumps (+), OFF tolerates downward jumps (-).
    # COOL: ON tolerates downward jumps (-), OFF tolerates upward jumps (+).
    if regime == WINDOW_REGIME_ON:
        return mode_sign * delta_sp < 0
    if regime == WINDOW_REGIME_OFF:
        return mode_sign * delta_sp > 0
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
    mode_sign: int = 1,
) -> int | None:
    """Return the earliest safe start index after the latest setpoint jump."""
    latest_jump_following_index: int | None = None
    for index in range(0, end_index + 1):
        if _blocks_regime_for_setpoint(
            observations[index],
            observations[index + 1],
            regime=regime,
            mode_sign=mode_sign,
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
        current_regime = classify_cycle_regime(observations[index].applied_demand)
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
