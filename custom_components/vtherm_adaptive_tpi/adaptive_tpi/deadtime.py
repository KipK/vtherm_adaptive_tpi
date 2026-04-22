"""Deadtime identification for Adaptive TPI using time-to-first-rise."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Mapping

CONFIDENCE_LOCK_THRESHOLD = 0.5

# Event detection
OFF_POWER_MAX_NEW = 0.15
STEP_POWER_MIN_NEW = 0.60
STEP_ABORT_POWER_NEW = 0.40
MAX_SETPOINT_JUMP = 0.30

# Rise detection
RISE_EPSILON = 0.10
RISE_EPSILON_STEP = 0.10
N_MAX_RISE_CYCLES = 8

# Quality
OFF_POWER_CLEAN = 0.05

# Aggregation
N_HIST = 6
N_LOCK_MIN = 1
SPREAD_MAX_CYCLES = 1.0

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
    bootstrap_b_learning_allowed: bool = False
    cycle_duration_min: float = 5.0


@dataclass(slots=True)
class StepIdentification:
    """Result of one step-response identification."""

    nd_cycles: float
    quality: float
    b_proxy: float | None
    cycle_index: int
    nd_minutes: float | None = None


@dataclass(slots=True)
class DeadtimeSearchResult:
    """Expose the deadtime identification outcome."""

    nd_hat: float
    nd_minutes: float | None
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


def _select_identification_for_nd_hat(
    identifications: list[StepIdentification],
    nd_hat: float,
) -> StepIdentification | None:
    """Return the identification that produced the selected deadtime in cycles."""
    if not identifications:
        return None

    total_quality = sum(ident.quality for ident in identifications)
    sorted_identifications = sorted(identifications, key=lambda ident: ident.nd_cycles)
    if total_quality > 0.0:
        cumulative = 0.0
        threshold = total_quality / 2.0
        for ident in sorted_identifications:
            cumulative += ident.quality
            if cumulative >= threshold:
                return ident
        return sorted_identifications[-1]

    return min(
        sorted_identifications,
        key=lambda ident: (abs(ident.nd_cycles - nd_hat), -ident.quality),
    )


def _find_latest_step(
    observations: tuple[CycleHistoryEntry, ...],
    last_processed_step_index: int,
) -> int | None:
    """Return the index of the latest unprocessed clean step or None."""
    n = len(observations)
    for step_index in range(n - 1, 0, -1):
        if step_index <= last_processed_step_index:
            break

        if observations[step_index].applied_power < STEP_POWER_MIN_NEW:
            continue
        if observations[step_index - 1].applied_power > OFF_POWER_MAX_NEW:
            continue

        return step_index

    return None


def _measure_rise_delay(
    observations: tuple[CycleHistoryEntry, ...],
    step_index: int,
    mode_sign: int = 1,
) -> StepIdentification | str:
    """Measure delay from step edge to first visible temperature response.

    In HEAT mode the response is a temperature rise; in COOL mode a drop.
    Returns _RESPONSE_PENDING while no response or abort has occurred yet,
    _RESPONSE_ABORTED on power drop or setpoint jump before the response,
    or a StepIdentification when a response (or ceiling) is detected.
    """
    tin_at_step = observations[step_index].tin
    n = len(observations)

    for n_cycles in range(1, n - step_index):
        i = step_index + n_cycles
        entry = observations[i]

        if entry.applied_power < STEP_ABORT_POWER_NEW:
            return _RESPONSE_ABORTED

        if abs(entry.target_temp - observations[i - 1].target_temp) > MAX_SETPOINT_JUMP:
            return _RESPONSE_ABORTED

        cumulative_rise = mode_sign * (entry.tin - tin_at_step)
        step_rise = mode_sign * (entry.tin - observations[i - 1].tin)

        rise_detected = cumulative_rise >= RISE_EPSILON or step_rise >= RISE_EPSILON_STEP
        ceiling_hit = n_cycles >= N_MAX_RISE_CYCLES

        if rise_detected or ceiling_hit:
            on_powers = [
                observations[step_index + j].applied_power
                for j in range(n_cycles + 1)
            ]
            q_power = min(1.0, sum(on_powers) / max(len(on_powers), 1))
            q_edge = (
                1.0
                if observations[step_index - 1].applied_power <= OFF_POWER_CLEAN
                else 0.7
            )
            quality = q_power * q_edge
            if ceiling_hit and not rise_detected:
                quality *= 0.5
            b_proxy = _compute_b_proxy(observations, step_index)
            nd_minutes = sum(
                max(0.0, float(observations[step_index + j].cycle_duration_min))
                for j in range(n_cycles)
            )
            return StepIdentification(
                nd_cycles=float(n_cycles),
                nd_minutes=nd_minutes,
                quality=quality,
                b_proxy=b_proxy,
                cycle_index=step_index,
            )

    return _RESPONSE_PENDING


def _compute_b_proxy(
    observations: tuple[CycleHistoryEntry, ...],
    step_index: int,
) -> float | None:
    """Estimate the thermal loss coefficient from the OFF period preceding the step."""
    off_slice = observations[max(0, step_index - 3) : step_index]
    measurements: list[float] = []
    for k in range(len(off_slice) - 1):
        delta_tin = off_slice[k + 1].tin - off_slice[k].tin
        delta_out = off_slice[k].tin - off_slice[k].tout
        if abs(delta_out) < MIN_B_DELTA_OUT:
            continue
        measurement = -delta_tin / delta_out
        if measurement > 0.0:
            measurements.append(measurement)
    if len(measurements) < 2:
        return None
    return sum(measurements) / len(measurements)


class DeadtimeModel:
    """Deadtime identifier using time-to-first-rise on step responses."""

    def __init__(self) -> None:
        """Initialize the deadtime identifier."""
        self._cycle_history: list[CycleHistoryEntry] = []
        self._identifications: deque[StepIdentification] = deque(maxlen=N_HIST)
        self._last_processed_step_index: int = -1
        self._pending_step_index: int | None = None
        self.nd_hat: float = 0.0
        self.nd_minutes: float | None = None
        self.confidence: float = 0.0
        self.locked: bool = False
        self.last_result: DeadtimeSearchResult = DeadtimeSearchResult(
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
        self.nd_minutes = None
        self.confidence = 0.0
        self.locked = False
        self.last_result = DeadtimeSearchResult(
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
        bootstrap_b_learning_allowed: bool = False,
        mode_sign: int = 1,
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
                bootstrap_b_learning_allowed=bootstrap_b_learning_allowed,
            )
        )
        self._trim_history_if_needed()

        if is_valid:
            self.last_result = self.evaluate(mode_sign=mode_sign)

        return self.last_result

    def evaluate(self, *, track_winner: bool = True, mode_sign: int = 1) -> DeadtimeSearchResult:
        """Scan for step identifications and recompute nd_hat."""
        del track_winner
        observations = tuple(self._cycle_history)

        if self._pending_step_index is not None:
            result = _measure_rise_delay(observations, self._pending_step_index, mode_sign)
            if isinstance(result, StepIdentification):
                self._identifications.append(result)
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
            self.nd_minutes = None
            self.confidence = 0.0
            self.locked = False
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

        nd_values = [ident.nd_cycles for ident in self._identifications]
        qualities = [ident.quality for ident in self._identifications]
        nd_hat = _weighted_median(nd_values, qualities)
        selected_identification = _select_identification_for_nd_hat(
            list(self._identifications),
            nd_hat,
        )
        nd_minutes = (
            selected_identification.nd_minutes
            if selected_identification is not None
            else None
        )

        if len(nd_values) >= 2:
            deviations = sorted(abs(nd - nd_hat) for nd in nd_values)
            spread = deviations[len(deviations) // 2]
        else:
            spread = 0.0

        n = len(nd_values)
        count_score = min(1.0, n / N_LOCK_MIN)
        spread_score = max(0.0, 1.0 - spread / SPREAD_MAX_CYCLES)
        quality_score = sum(qualities) / len(qualities)
        confidence = count_score * spread_score * quality_score

        lock_reason: str | None = None
        if n < N_LOCK_MIN:
            lock_reason = "deadtime_insufficient_identifications"
        elif spread > SPREAD_MAX_CYCLES:
            lock_reason = "deadtime_insufficient_separation"
        elif confidence < CONFIDENCE_LOCK_THRESHOLD:
            lock_reason = "deadtime_confidence_low"

        locked = lock_reason is None
        self.nd_hat = nd_hat
        self.nd_minutes = nd_minutes
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
            nd_minutes=nd_minutes,
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
                    "nd_minutes": ident.nd_minutes,
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
                    nd_minutes_raw = raw.get("nd_minutes")
                    self._identifications.append(
                        StepIdentification(
                            nd_cycles=float(raw["nd_cycles"]),
                            nd_minutes=(
                                float(nd_minutes_raw)
                                if nd_minutes_raw is not None
                                else None
                            ),
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
