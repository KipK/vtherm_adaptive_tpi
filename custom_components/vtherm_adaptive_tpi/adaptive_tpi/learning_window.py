"""Lightweight cycle-aligned learning windows for Adaptive TPI."""

from __future__ import annotations

from dataclasses import dataclass
from math import floor

from .deadtime import CycleHistoryEntry

WINDOW_REGIME_OFF = "off"
WINDOW_REGIME_ON = "on"

OFF_POWER_MAX = 0.10
ON_POWER_MIN = 0.25
MIN_WINDOW_AMPLITUDE = 0.08
MIN_WINDOW_DURATION_MIN = 8.0
MAX_WINDOW_CYCLES = 3
MAX_WINDOW_DURATION_MIN = 45.0


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

    candidate = _find_latest_candidate(observations)
    if candidate is None:
        return LearningWindowResult(
            sample=None,
            reason="window_no_candidate",
            waiting_next_cycle=False,
        )
    end_index, detected_regime = candidate
    if detected_regime != regime:
        return LearningWindowResult(
            sample=None,
            reason=f"{regime}_window_inactive",
            waiting_next_cycle=False,
        )

    start_index = end_index
    total_duration_min = _duration_minutes(observations[end_index])
    cycle_count = 1
    while start_index > 0 and cycle_count < MAX_WINDOW_CYCLES:
        previous = observations[start_index - 1]
        if not previous.is_estimator_informative or not _matches_regime(previous, regime):
            break
        next_duration = total_duration_min + _duration_minutes(previous)
        if next_duration > MAX_WINDOW_DURATION_MIN:
            break
        start_index -= 1
        total_duration_min = next_duration
        cycle_count += 1

    end_observation = observations[end_index + 1]
    start_observation = observations[start_index]
    amplitude = end_observation.tin - start_observation.tin
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
            dTdt=amplitude / total_duration_min,
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
    )


def _find_latest_candidate(
    observations: tuple[CycleHistoryEntry, ...],
) -> tuple[int, str] | None:
    for candidate_index in range(len(observations) - 2, -1, -1):
        current = observations[candidate_index]
        nxt = observations[candidate_index + 1]
        if not current.is_estimator_informative or not nxt.is_valid:
            continue
        if current.applied_power <= OFF_POWER_MAX:
            return candidate_index, WINDOW_REGIME_OFF
        if current.applied_power >= ON_POWER_MIN:
            return candidate_index, WINDOW_REGIME_ON
    return None


def _matches_regime(entry: CycleHistoryEntry, regime: str) -> bool:
    if regime == WINDOW_REGIME_OFF:
        return entry.applied_power <= OFF_POWER_MAX
    if regime == WINDOW_REGIME_ON:
        return entry.applied_power >= ON_POWER_MIN
    return False


def _duration_minutes(entry: CycleHistoryEntry) -> float:
    return max(0.0, float(entry.cycle_duration_min))
