"""Temporal deadtime tracking based on raw temperature measurements."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .deadtime import (
    MAX_SETPOINT_JUMP,
    N_MAX_RISE_CYCLES,
    OFF_POWER_CLEAN,
    OFF_POWER_MAX_NEW,
    RISE_EPSILON,
    STEP_ABORT_POWER_NEW,
    STEP_POWER_MIN_NEW,
    StepIdentification,
)

TEMPORAL_CONFIRMATION_POINTS = 1


@dataclass(slots=True)
class _TrackedStep:
    """Store one active time-domain step response."""

    started_at: datetime
    start_temp: float
    target_temp: float
    cycle_duration_min: float
    mode_sign: int
    on_percent: float
    previous_on_percent: float
    cycle_index: int
    b_proxy: float | None
    last_measured_at: datetime
    last_temp: float
    candidate_started_at: datetime | None = None
    candidate_points_count: int = 0

    @property
    def base_quality(self) -> float:
        """Return the base identification quality for this clean step."""
        q_power = min(1.0, self.on_percent)
        q_edge = 1.0 if self.previous_on_percent <= OFF_POWER_CLEAN else 0.7
        return q_power * q_edge

    @property
    def max_wait_minutes(self) -> float:
        """Return the maximum wait before producing a ceiling identification."""
        return max(0.0, self.cycle_duration_min) * N_MAX_RISE_CYCLES


class DeadtimeTracker:
    """Track deadtime from raw temperature measurements instead of cycle boundaries."""

    def __init__(self, *, now_provider: Callable[[], datetime]) -> None:
        """Initialize the tracker."""
        self._now_provider = now_provider
        self.reset()

    def reset(self) -> None:
        """Reset the current tracked step."""
        self._step: _TrackedStep | None = None

    @property
    def has_pending_step(self) -> bool:
        """Return True while a temporal step response is being tracked."""
        return self._step is not None

    def on_cycle_started(
        self,
        *,
        started_at: datetime | None,
        current_temp: float | None,
        target_temp: float | None,
        on_percent: float,
        previous_on_percent: float,
        mode_sign: int,
        cycle_duration_min: float,
        cycle_index: int,
        b_proxy: float | None,
    ) -> None:
        """Arm or abort temporal deadtime tracking when a cycle starts."""
        started_at = self._normalize_timestamp(started_at)
        current_on_percent = max(0.0, min(1.0, float(on_percent)))
        previous_on_percent = max(0.0, min(1.0, float(previous_on_percent)))

        if self._step is not None:
            if (
                mode_sign != self._step.mode_sign
                or target_temp is None
                or abs(float(target_temp) - self._step.target_temp) > MAX_SETPOINT_JUMP
                or current_on_percent < STEP_ABORT_POWER_NEW
            ):
                self.reset()
            else:
                self._step.on_percent = max(self._step.on_percent, current_on_percent)
                self._step.cycle_duration_min = max(
                    self._step.cycle_duration_min,
                    max(0.0, float(cycle_duration_min)),
                )
                return

        if (
            mode_sign == 0
            or current_temp is None
            or target_temp is None
            or current_on_percent < STEP_POWER_MIN_NEW
            or previous_on_percent > OFF_POWER_MAX_NEW
        ):
            return

        current_temp = float(current_temp)
        target_temp = float(target_temp)
        self._step = _TrackedStep(
            started_at=started_at,
            start_temp=current_temp,
            target_temp=target_temp,
            cycle_duration_min=max(0.0, float(cycle_duration_min)),
            mode_sign=mode_sign,
            on_percent=current_on_percent,
            previous_on_percent=previous_on_percent,
            cycle_index=cycle_index,
            b_proxy=b_proxy,
            last_measured_at=started_at,
            last_temp=current_temp,
        )

    def observe_temperature(
        self,
        *,
        measured_at: datetime | None,
        current_temp: float | None,
        target_temp: float | None,
        mode_sign: int,
    ) -> StepIdentification | None:
        """Consume one raw temperature measurement and confirm a real slope onset."""
        step = self._step
        if (
            step is None
            or current_temp is None
            or target_temp is None
            or mode_sign != step.mode_sign
            or mode_sign == 0
        ):
            return None

        if abs(float(target_temp) - step.target_temp) > MAX_SETPOINT_JUMP:
            self.reset()
            return None

        measured_at = self._normalize_timestamp(measured_at)
        if measured_at <= step.started_at or measured_at <= step.last_measured_at:
            return None

        current_temp = float(current_temp)
        response_delta = step.mode_sign * (current_temp - step.start_temp)
        incremental_delta = step.mode_sign * (current_temp - step.last_temp)
        elapsed_min = max(
            0.0,
            (measured_at - step.started_at).total_seconds() / 60.0,
        )

        if step.candidate_started_at is None:
            if incremental_delta > 0.0 and response_delta > 0.0:
                step.candidate_started_at = measured_at
                step.candidate_points_count = 1
        elif incremental_delta > 0.0:
            step.candidate_points_count += 1
        elif incremental_delta < 0.0:
            step.candidate_started_at = None
            step.candidate_points_count = 0

        if (
            step.candidate_started_at is not None
            and step.candidate_points_count >= TEMPORAL_CONFIRMATION_POINTS
            and response_delta >= RISE_EPSILON
        ):
            identification = self._build_identification(
                step=step,
                detected_at=step.candidate_started_at,
                quality=step.base_quality,
            )
            self.reset()
            return identification

        if (
            step.max_wait_minutes > 0.0
            and elapsed_min >= step.max_wait_minutes
        ):
            identification = self._build_identification(
                step=step,
                detected_at=step.started_at,
                quality=step.base_quality * 0.5,
                force_nd_minutes=step.max_wait_minutes,
            )
            self.reset()
            return identification

        step.last_measured_at = measured_at
        step.last_temp = current_temp
        return None

    def _build_identification(
        self,
        *,
        step: _TrackedStep,
        detected_at: datetime,
        quality: float,
        force_nd_minutes: float | None = None,
    ) -> StepIdentification:
        """Convert one confirmed onset into a persisted deadtime identification."""
        nd_minutes = (
            force_nd_minutes
            if force_nd_minutes is not None
            else max(0.0, (detected_at - step.started_at).total_seconds() / 60.0)
        )
        nd_cycles = (
            nd_minutes / step.cycle_duration_min
            if step.cycle_duration_min > 0.0
            else 0.0
        )
        return StepIdentification(
            nd_cycles=nd_cycles,
            nd_minutes=nd_minutes,
            quality=max(0.0, min(1.0, quality)),
            b_proxy=step.b_proxy,
            cycle_index=step.cycle_index,
        )

    def _normalize_timestamp(self, measured_at: datetime | None) -> datetime:
        """Return a usable timestamp for raw-measurement tracking."""
        if isinstance(measured_at, datetime):
            return measured_at
        return self._now_provider()
