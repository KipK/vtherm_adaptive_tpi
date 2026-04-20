"""Deadtime identification for Adaptive TPI using exponentially-weighted moments."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any, Mapping

CONFIDENCE_LOCK_THRESHOLD = 0.6

# Step detection
N_OFF_MIN = 2
STEP_POWER_MIN = 0.70
OFF_POWER_MAX = 0.15
MAX_SETPOINT_JUMP = 0.3

# Response collection
STEP_ABORT_POWER = 0.50
N_MAX_COLLECT = 20
PLATEAU_WINDOW = 3
PLATEAU_THRESHOLD = 0.02
MIN_AMPLITUDE = 0.30
TARGET_AMPLITUDE = 1.0

# Confidence and locking
N_HIST = 6
N_LOCK_MIN = 3
MAX_REL_SPREAD = 0.30

# History management
MAX_HISTORY_LEN = 100
TRIM_TO_LEN = 60

# b proxy
MIN_B_DELTA_OUT = 1.0

_RESPONSE_PENDING = "pending"
_RESPONSE_ABORTED = "aborted"


@dataclass(slots=True)
class DeadtimeObservation:
    """One accepted cycle sample used by the deadtime identifier."""

    tin: float
    tout: float
    target_temp: float
    applied_power: float


@dataclass(slots=True)
class CycleHistoryEntry:
    """One real scheduler cycle kept for temporal alignment."""

    tin: float
    tout: float
    target_temp: float
    applied_power: float
    is_valid: bool
    is_informative: bool
    is_estimator_informative: bool
    cycle_duration_min: float = 5.0


@dataclass(slots=True)
class StepIdentification:
    """Result of one step-response identification."""

    nd_cycles: float
    quality: float
    b_proxy: float | None
    cycle_index: int


@dataclass(slots=True)
class DeadtimeSearchResult:
    """Expose the deadtime identification outcome."""

    nd_hat: float
    c_nd: float
    locked: bool
    best_candidate: float | None
    second_best_candidate: float | None
    best_candidate_a: float | None
    best_candidate_b: float | None
    candidate_costs: dict[str, float]
    lock_reason: str | None


def _weighted_median(values: list[float], weights: list[float]) -> float:
    """Return a weighted median of values."""
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    total = sum(weights)
    if total <= 0.0:
        return sum(values) / len(values)
    sorted_pairs = sorted(zip(values, weights), key=lambda p: p[0])
    cumulative = 0.0
    for val, w in sorted_pairs:
        cumulative += w
        if cumulative >= total / 2.0:
            return val
    return sorted_pairs[-1][0]


def _find_latest_step(
    observations: tuple[CycleHistoryEntry, ...],
    last_processed_step_index: int,
) -> int | None:
    """Return the index of the latest unprocessed clean step or None."""
    n = len(observations)
    for step_index in range(n - 1, N_OFF_MIN, -1):
        if step_index <= last_processed_step_index:
            break

        if observations[step_index].applied_power < STEP_POWER_MIN:
            continue
        if observations[step_index - 1].applied_power > OFF_POWER_MAX:
            continue

        off_slice = observations[step_index - N_OFF_MIN : step_index]
        if not all(e.applied_power <= OFF_POWER_MAX for e in off_slice):
            continue

        guard_start = max(0, step_index - N_OFF_MIN)
        guard_end = min(n, step_index + 3)
        guard_slice = observations[guard_start:guard_end]
        if any(
            abs(guard_slice[k].target_temp - guard_slice[k - 1].target_temp)
            > MAX_SETPOINT_JUMP
            for k in range(1, len(guard_slice))
        ):
            continue

        return step_index

    return None


def _collect_response(
    observations: tuple[CycleHistoryEntry, ...],
    step_index: int,
) -> list[CycleHistoryEntry] | str:
    """Collect step-response cycles starting at step_index.

    Returns the collected list when a plateau is detected,
    _RESPONSE_PENDING while still accumulating, or _RESPONSE_ABORTED on failure.
    """
    response: list[CycleHistoryEntry] = []
    for i in range(step_index, len(observations)):
        entry = observations[i]

        if i > step_index and entry.applied_power < STEP_ABORT_POWER:
            return _RESPONSE_ABORTED

        response.append(entry)

        if len(response) > N_MAX_COLLECT:
            return _RESPONSE_ABORTED

        if len(response) >= PLATEAU_WINDOW + 1:
            last_dts = [
                abs(response[j].tin - response[j - 1].tin)
                for j in range(len(response) - PLATEAU_WINDOW, len(response))
            ]
            if all(dt < PLATEAU_THRESHOLD for dt in last_dts):
                return response

    return _RESPONSE_PENDING


def _compute_weighted_moments(
    response: list[CycleHistoryEntry],
) -> tuple[float, float] | None:
    """Extract (nd_cycles, quality) from a collected step response.

    Applies exponentially-weighted moments of the normalised step-response curve:
        M0 = Σ w(k)·(1−y(k))·Δt,  M1 = Σ w(k)·t_k·(1−y(k))·Δt
    then derives L = max(0, T_ar − T) where T_ar = M1/M0 and T = sqrt(M2/M0 − T_ar²).
    Returns None when the response does not meet quality requirements.
    """
    if len(response) < 4:
        return None

    tin_0 = response[0].tin
    tail_len = min(3, len(response))
    tin_final_est = sum(e.tin for e in response[-tail_len:]) / tail_len
    amplitude = tin_final_est - tin_0

    if amplitude < MIN_AMPLITUDE:
        return None

    delta_ts = [e.cycle_duration_min * 60.0 for e in response]
    mean_dt = sum(delta_ts) / len(delta_ts)

    cumulative_times: list[float] = []
    t = 0.0
    for dt in delta_ts:
        cumulative_times.append(t)
        t += dt

    alpha = 1.0 / max(t, 1.0)

    m0 = m1 = m2 = 0.0
    for entry, t_k, dt_k in zip(response, cumulative_times, delta_ts):
        y_norm = (entry.tin - tin_0) / amplitude
        complement = max(0.0, 1.0 - y_norm)
        w = math.exp(-alpha * t_k)
        m0 += w * complement * dt_k
        m1 += w * t_k * complement * dt_k
        m2 += w * t_k * t_k * complement * dt_k

    if m0 < 1e-6:
        return None

    t_ar = m1 / m0
    variance = m2 / m0 - t_ar * t_ar
    time_const = math.sqrt(max(variance, 0.0))
    l_seconds = max(0.0, t_ar - time_const)
    nd_cycles = l_seconds / max(mean_dt, 1.0)

    q_amplitude = min(1.0, amplitude / TARGET_AMPLITUDE)
    n_on = sum(1 for e in response if e.applied_power >= STEP_POWER_MIN * 0.85)
    q_power = n_on / len(response)
    quality = q_amplitude * q_power

    return nd_cycles, quality


def _compute_b_proxy(
    observations: tuple[CycleHistoryEntry, ...],
    step_index: int,
) -> float | None:
    """Estimate the thermal loss coefficient from the OFF period preceding the step."""
    off_slice = observations[max(0, step_index - N_OFF_MIN) : step_index]
    measurements: list[float] = []
    for k in range(len(off_slice) - 1):
        delta_tin = off_slice[k + 1].tin - off_slice[k].tin
        delta_out = off_slice[k].tin - off_slice[k].tout
        if abs(delta_out) < MIN_B_DELTA_OUT:
            continue
        measurement = -delta_tin / delta_out
        if measurement > 0.0:
            measurements.append(measurement)
    if not measurements:
        return None
    return sum(measurements) / len(measurements)


class DeadtimeModel:
    """Deadtime identifier using exponentially-weighted moments on step responses."""

    def __init__(self) -> None:
        """Initialize the deadtime identifier."""
        self._cycle_history: list[CycleHistoryEntry] = []
        self._identifications: deque[StepIdentification] = deque(maxlen=N_HIST)
        self._last_processed_step_index: int = -1
        self._pending_step_index: int | None = None
        self.nd_hat: float = 0.0
        self.confidence: float = 0.0
        self.locked: bool = False
        self.last_result: DeadtimeSearchResult = DeadtimeSearchResult(
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

    @property
    def accepted_cycle_count(self) -> int:
        """Return the number of informative cycles in the current session."""
        return sum(1 for e in self._cycle_history if e.is_informative)

    @property
    def accepted_observations(self) -> tuple[DeadtimeObservation, ...]:
        """Expose informative observations for backward-compatible callers."""
        return tuple(
            DeadtimeObservation(
                tin=e.tin,
                tout=e.tout,
                target_temp=e.target_temp,
                applied_power=e.applied_power,
            )
            for e in self._cycle_history
            if e.is_informative
        )

    @property
    def cycle_history(self) -> tuple[CycleHistoryEntry, ...]:
        """Expose the complete cycle history for learning window construction."""
        return tuple(self._cycle_history)

    def reset(self) -> None:
        """Reset all state."""
        self._cycle_history.clear()
        self._identifications.clear()
        self._last_processed_step_index = -1
        self._pending_step_index = None
        self.nd_hat = 0.0
        self.confidence = 0.0
        self.locked = False
        self.last_result = DeadtimeSearchResult(
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

    def record_accepted_observation(
        self,
        observation: DeadtimeObservation,
    ) -> DeadtimeSearchResult:
        """Append an accepted observation and recompute deadtime."""
        return self.record_cycle(
            observation,
            is_valid=True,
            is_informative=True,
            is_estimator_informative=True,
        )

    def record_cycle(
        self,
        observation: DeadtimeObservation,
        *,
        cycle_duration_min: float = 5.0,
        is_valid: bool,
        is_informative: bool,
        is_estimator_informative: bool = False,
    ) -> DeadtimeSearchResult:
        """Append one cycle and trigger step detection when valid."""
        self._cycle_history.append(
            CycleHistoryEntry(
                tin=observation.tin,
                tout=observation.tout,
                target_temp=observation.target_temp,
                applied_power=observation.applied_power,
                cycle_duration_min=cycle_duration_min,
                is_valid=is_valid,
                is_informative=is_informative,
                is_estimator_informative=is_estimator_informative,
            )
        )
        self._trim_history_if_needed()

        if is_valid:
            self.last_result = self.evaluate()

        return self.last_result

    def evaluate(self, *, track_winner: bool = True) -> DeadtimeSearchResult:
        """Scan for step identifications and recompute nd_hat."""
        del track_winner
        observations = tuple(self._cycle_history)

        if self._pending_step_index is not None:
            result = _collect_response(observations, self._pending_step_index)
            if isinstance(result, list):
                moments = _compute_weighted_moments(result)
                if moments is not None:
                    nd_cycles, quality = moments
                    b_proxy = _compute_b_proxy(observations, self._pending_step_index)
                    self._identifications.append(
                        StepIdentification(
                            nd_cycles=nd_cycles,
                            quality=quality,
                            b_proxy=b_proxy,
                            cycle_index=self._pending_step_index,
                        )
                    )
                self._last_processed_step_index = self._pending_step_index
                self._pending_step_index = None
            elif result == _RESPONSE_ABORTED:
                self._last_processed_step_index = self._pending_step_index
                self._pending_step_index = None

        if self._pending_step_index is None:
            step_index = _find_latest_step(
                observations, self._last_processed_step_index
            )
            if step_index is not None:
                self._pending_step_index = step_index

        self.last_result = self._recompute_nd_hat()
        return self.last_result

    def _recompute_nd_hat(self) -> DeadtimeSearchResult:
        """Derive nd_hat, confidence and lock state from stored identifications."""
        if not self._identifications:
            self.nd_hat = 0.0
            self.confidence = 0.0
            self.locked = False
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

        nd_values = [ident.nd_cycles for ident in self._identifications]
        qualities = [ident.quality for ident in self._identifications]
        nd_hat = _weighted_median(nd_values, qualities)

        if len(nd_values) >= 2:
            deviations = sorted(abs(nd - nd_hat) for nd in nd_values)
            spread = deviations[len(deviations) // 2]
            rel_spread = spread / max(nd_hat, 0.1)
        else:
            rel_spread = 1.0

        n = len(nd_values)
        count_score = min(1.0, n / N_LOCK_MIN)
        spread_score = max(0.0, 1.0 - rel_spread / MAX_REL_SPREAD)
        quality_score = sum(qualities) / len(qualities)
        confidence = count_score * spread_score * quality_score

        lock_reason: str | None = None
        if n < N_LOCK_MIN:
            lock_reason = "deadtime_insufficient_identifications"
        elif rel_spread >= MAX_REL_SPREAD:
            lock_reason = "deadtime_insufficient_separation"
        elif confidence < CONFIDENCE_LOCK_THRESHOLD:
            lock_reason = "deadtime_confidence_low"

        locked = lock_reason is None
        self.nd_hat = nd_hat
        self.confidence = confidence
        self.locked = locked

        candidate_costs = {
            str(i): ident.quality for i, ident in enumerate(self._identifications)
        }
        sorted_by_quality = sorted(
            self._identifications, key=lambda x: x.quality, reverse=True
        )
        best = sorted_by_quality[0]
        second = sorted_by_quality[1] if len(sorted_by_quality) > 1 else None

        return DeadtimeSearchResult(
            nd_hat=nd_hat,
            c_nd=confidence,
            locked=locked,
            best_candidate=best.nd_cycles,
            second_best_candidate=second.nd_cycles if second else None,
            best_candidate_a=None,
            best_candidate_b=best.b_proxy,
            candidate_costs=candidate_costs,
            lock_reason=lock_reason,
        )

    def _trim_history_if_needed(self) -> None:
        """Remove oldest history entries and adjust tracked indices accordingly."""
        if len(self._cycle_history) <= MAX_HISTORY_LEN:
            return
        trim_count = len(self._cycle_history) - TRIM_TO_LEN
        del self._cycle_history[:trim_count]
        self._last_processed_step_index = max(
            -1, self._last_processed_step_index - trim_count
        )
        if self._pending_step_index is not None:
            self._pending_step_index -= trim_count
            if self._pending_step_index < 0:
                self._pending_step_index = None

    def to_persisted_dict(self) -> dict[str, Any]:
        """Serialize identified deadtimes for warm restarts."""
        return {
            "identifications": [
                {
                    "nd_cycles": ident.nd_cycles,
                    "quality": ident.quality,
                    "b_proxy": ident.b_proxy,
                    "cycle_index": ident.cycle_index,
                }
                for ident in self._identifications
            ],
            "last_processed_step_index": self._last_processed_step_index,
        }

    def load_persisted_dict(self, data: Mapping[str, Any] | None) -> None:
        """Restore identifications from a persisted snapshot."""
        self.reset()
        if not isinstance(data, Mapping):
            return
        # Silently discard the old candidate-regression format
        if "cycle_history" in data:
            return

        raw_idents = data.get("identifications", [])
        if isinstance(raw_idents, list):
            for raw in raw_idents:
                if not isinstance(raw, Mapping):
                    continue
                try:
                    b_raw = raw.get("b_proxy")
                    self._identifications.append(
                        StepIdentification(
                            nd_cycles=float(raw["nd_cycles"]),
                            quality=float(raw["quality"]),
                            b_proxy=float(b_raw) if b_raw is not None else None,
                            cycle_index=int(raw.get("cycle_index", 0)),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue

        lp = data.get("last_processed_step_index")
        if isinstance(lp, int):
            self._last_processed_step_index = lp

        if self._identifications:
            self.last_result = self._recompute_nd_hat()
