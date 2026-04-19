"""Deadtime search primitives for Adaptive TPI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

A_FLOOR = 1e-3
CONFIDENCE_LOCK_THRESHOLD = 0.6
LOCK_MIN_ACCEPTED_CYCLES = 10
LOCK_MIN_DOMINANCE_RATIO = 2.0
LOCK_RECENT_WINDOW = 10
LOCK_RECENT_BEST_COUNT = 7

THERMOSTAT_CLASS_FAST_ELECTRIC = "fast_electric"
THERMOSTAT_CLASS_HYDRONIC = "hydronic"
THERMOSTAT_CLASS_UNKNOWN = "unknown"

CANDIDATE_SETS: dict[str, tuple[int, ...]] = {
    THERMOSTAT_CLASS_FAST_ELECTRIC: (0, 1, 2, 3),
    THERMOSTAT_CLASS_HYDRONIC: (0, 1, 2, 3, 4, 5, 6),
    THERMOSTAT_CLASS_UNKNOWN: (0, 1, 2, 3, 4, 5, 6),
}


@dataclass(slots=True)
class DeadtimeObservation:
    """One accepted cycle sample used by the coarse deadtime search."""

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
class DeadtimeCandidateScore:
    """Candidate deadtime score with its temporary constrained fit."""

    candidate: int
    cost: float
    a_c: float
    b_c: float
    sample_count: int


@dataclass(slots=True)
class DeadtimeSearchResult:
    """Expose the coarse deadtime search outcome."""

    nd_hat: float
    c_nd: float
    locked: bool
    best_candidate: float | None
    second_best_candidate: float | None
    best_candidate_a: float | None
    best_candidate_b: float | None
    candidate_costs: dict[str, float]
    lock_reason: str | None


class DeadtimeModel:
    """Standalone coarse deadtime search state."""

    def __init__(self, thermostat_class: str = THERMOSTAT_CLASS_UNKNOWN) -> None:
        """Initialize the deadtime search model."""
        self._thermostat_class = thermostat_class
        self._cycle_history: list[CycleHistoryEntry] = []
        self._best_candidate_history: list[int] = []
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
            lock_reason="insufficient_data",
        )

    @property
    def accepted_cycle_count(self) -> int:
        """Return the number of accepted observations tracked by the model."""
        return sum(1 for entry in self._cycle_history if entry.is_informative)

    @property
    def accepted_observations(self) -> tuple[DeadtimeObservation, ...]:
        """Expose the informative observations kept for backward-compatible callers."""
        return tuple(
            DeadtimeObservation(
                tin=entry.tin,
                tout=entry.tout,
                target_temp=entry.target_temp,
                applied_power=entry.applied_power,
            )
            for entry in self._cycle_history
            if entry.is_informative
        )

    @property
    def cycle_history(self) -> tuple[CycleHistoryEntry, ...]:
        """Expose the complete cycle history for temporally aligned learning."""
        return tuple(self._cycle_history)

    def reset(self) -> None:
        """Reset the deadtime state."""
        self._cycle_history.clear()
        self._best_candidate_history.clear()
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
            lock_reason="insufficient_data",
        )

    def to_persisted_dict(self) -> dict[str, Any]:
        """Serialize the deadtime model so warm restarts preserve learning continuity."""
        return {
            "thermostat_class": self._thermostat_class,
            "cycle_history": [
                {
                    "tin": entry.tin,
                    "tout": entry.tout,
                    "target_temp": entry.target_temp,
                    "applied_power": entry.applied_power,
                    "is_valid": entry.is_valid,
                    "is_informative": entry.is_informative,
                    "is_estimator_informative": entry.is_estimator_informative,
                    "cycle_duration_min": entry.cycle_duration_min,
                }
                for entry in self._cycle_history
            ],
            "best_candidate_history": list(self._best_candidate_history),
        }

    def load_persisted_dict(self, data: Mapping[str, Any] | None) -> None:
        """Restore the deadtime model from persisted payload."""
        self.reset()
        if not isinstance(data, Mapping):
            return

        thermostat_class = data.get("thermostat_class")
        if isinstance(thermostat_class, str) and thermostat_class in CANDIDATE_SETS:
            self._thermostat_class = thermostat_class

        loaded_history: list[CycleHistoryEntry] = []
        raw_history = data.get("cycle_history")
        if isinstance(raw_history, list):
            for raw_entry in raw_history:
                if not isinstance(raw_entry, Mapping):
                    continue
                try:
                    loaded_history.append(
                        CycleHistoryEntry(
                            tin=float(raw_entry["tin"]),
                            tout=float(raw_entry["tout"]),
                            target_temp=float(raw_entry["target_temp"]),
                            applied_power=float(raw_entry["applied_power"]),
                            is_valid=bool(raw_entry["is_valid"]),
                            is_informative=bool(raw_entry["is_informative"]),
                            is_estimator_informative=bool(raw_entry["is_estimator_informative"]),
                            cycle_duration_min=float(raw_entry.get("cycle_duration_min", 5.0)),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
        self._cycle_history = loaded_history

        raw_best_history = data.get("best_candidate_history")
        if isinstance(raw_best_history, list):
            self._best_candidate_history = [
                int(candidate)
                for candidate in raw_best_history
                if isinstance(candidate, (int, float)) and int(candidate) in self.candidate_set()
            ]

        if self._cycle_history:
            self.last_result = self.evaluate(track_winner=False)

    def record_accepted_observation(
        self,
        observation: DeadtimeObservation,
    ) -> DeadtimeSearchResult:
        """Append an accepted observation and recompute coarse deadtime scores."""
        self.last_result = self.record_cycle(
            observation,
            is_valid=True,
            is_informative=True,
            is_estimator_informative=True,
        )
        return self.last_result

    def record_cycle(
        self,
        observation: DeadtimeObservation,
        *,
        cycle_duration_min: float = 5.0,
        is_valid: bool,
        is_informative: bool,
        is_estimator_informative: bool = False,
    ) -> DeadtimeSearchResult:
        """Append one real cycle while preserving temporal alignment."""
        previous_entry = self._cycle_history[-1] if self._cycle_history else None
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
        self.last_result = self.evaluate(
            track_winner=bool(is_valid and previous_entry is not None and previous_entry.is_informative)
        )
        return self.last_result

    def evaluate(self, *, track_winner: bool = True) -> DeadtimeSearchResult:
        """Evaluate the candidate deadtime set over accepted observations."""
        scores = self._score_candidates()
        if not scores:
            self.locked = False
            self.confidence = 0.0
            self.last_result = DeadtimeSearchResult(
                nd_hat=self.nd_hat,
                c_nd=0.0,
                locked=False,
                best_candidate=None,
                second_best_candidate=None,
                best_candidate_a=None,
                best_candidate_b=None,
                candidate_costs={},
                lock_reason="insufficient_data",
            )
            return self.last_result

        sorted_scores = sorted(scores, key=lambda score: score.cost)
        best_score = sorted_scores[0]
        second_score = sorted_scores[1] if len(sorted_scores) > 1 else None
        if track_winner:
            self._best_candidate_history.append(best_score.candidate)

        ratio = self._dominance_ratio(best_score.cost, second_score.cost if second_score else None)
        consistency_all = self._consistency_score(best_score.candidate)
        recent_best_count = self._recent_best_count(best_score.candidate)
        confidence = self._compute_confidence(
            accepted_cycle_count=self.accepted_cycle_count,
            ratio=ratio,
            consistency_all=consistency_all,
        )
        lock_reason = self._lock_reason(
            accepted_cycle_count=self.accepted_cycle_count,
            ratio=ratio,
            recent_best_count=recent_best_count,
        )
        locked = lock_reason is None

        self.nd_hat = float(best_score.candidate)
        self.confidence = confidence
        self.locked = locked
        self.last_result = DeadtimeSearchResult(
            nd_hat=self.nd_hat,
            c_nd=confidence,
            locked=locked,
            best_candidate=float(best_score.candidate),
            second_best_candidate=(float(second_score.candidate) if second_score else None),
            best_candidate_a=best_score.a_c,
            best_candidate_b=best_score.b_c,
            candidate_costs={str(score.candidate): score.cost for score in sorted_scores},
            lock_reason=lock_reason,
        )
        return self.last_result

    def candidate_set(self) -> tuple[int, ...]:
        """Return the coarse deadtime candidates for the configured thermostat class."""
        return CANDIDATE_SETS.get(self._thermostat_class, CANDIDATE_SETS[THERMOSTAT_CLASS_UNKNOWN])

    def _score_candidates(self) -> list[DeadtimeCandidateScore]:
        """Score each candidate deadtime with a constrained least-squares fit."""
        scores: list[DeadtimeCandidateScore] = []
        for candidate in self.candidate_set():
            rows = self._candidate_rows(candidate)
            if len(rows) < 2:
                continue

            fit = self._solve_constrained_pair(rows)
            if fit is None:
                continue

            a_c, b_c = fit
            residual_sum = 0.0
            for u_del, loss_term, delta_tin in rows:
                residual = delta_tin - (a_c * u_del + b_c * loss_term)
                residual_sum += residual * residual

            scores.append(
                DeadtimeCandidateScore(
                    candidate=candidate,
                    cost=residual_sum / len(rows),
                    a_c=a_c,
                    b_c=b_c,
                    sample_count=len(rows),
                )
            )

        return scores

    def _candidate_rows(self, candidate: int) -> list[tuple[float, float, float]]:
        """Build the regression rows for one candidate deadtime."""
        observations = self._cycle_history
        rows: list[tuple[float, float, float]] = []
        for index in range(candidate, len(observations) - 1):
            current = observations[index]
            next_observation = observations[index + 1]
            if not current.is_informative or not next_observation.is_valid:
                continue
            delayed_source = observations[index - candidate]
            delta_tin = next_observation.tin - current.tin
            loss_term = -(current.tin - current.tout)
            rows.append((delayed_source.applied_power, loss_term, delta_tin))
        return rows

    @staticmethod
    def _solve_constrained_pair(
        rows: list[tuple[float, float, float]],
    ) -> tuple[float, float] | None:
        """Solve the temporary least-squares pair under the simple physical bounds."""
        sum_x1x1 = 0.0
        sum_x1x2 = 0.0
        sum_x2x2 = 0.0
        sum_x1y = 0.0
        sum_x2y = 0.0

        for x1, x2, y in rows:
            sum_x1x1 += x1 * x1
            sum_x1x2 += x1 * x2
            sum_x2x2 += x2 * x2
            sum_x1y += x1 * y
            sum_x2y += x2 * y

        determinant = (sum_x1x1 * sum_x2x2) - (sum_x1x2 * sum_x1x2)
        if abs(determinant) < 1e-9:
            return None

        a_c = ((sum_x1y * sum_x2x2) - (sum_x2y * sum_x1x2)) / determinant
        b_c = ((sum_x1x1 * sum_x2y) - (sum_x1x2 * sum_x1y)) / determinant
        if b_c >= 0.0:
            return max(A_FLOOR, a_c), b_c

        # When the unconstrained fit pushes b below zero, the physically valid
        # projection is the one-dimensional refit with b fixed to 0.
        if sum_x1x1 < 1e-9:
            return A_FLOOR, 0.0
        a_proj = sum_x1y / sum_x1x1
        return max(A_FLOOR, a_proj), 0.0

    @staticmethod
    def _dominance_ratio(best_cost: float, second_cost: float | None) -> float:
        """Return the ratio between the second-best and best candidate costs."""
        if second_cost is None:
            return 0.0
        return second_cost / max(best_cost, 1e-9)

    def _consistency_score(self, best_candidate: int) -> float:
        """Return how consistently one candidate has been winning."""
        if not self._best_candidate_history:
            return 0.0
        return self._best_candidate_history.count(best_candidate) / len(self._best_candidate_history)

    def _recent_best_count(self, best_candidate: int) -> int:
        """Return the number of recent accepted cycles won by the best candidate."""
        recent_history = self._best_candidate_history[-LOCK_RECENT_WINDOW:]
        return recent_history.count(best_candidate)

    @staticmethod
    def _compute_confidence(
        *,
        accepted_cycle_count: int,
        ratio: float,
        consistency_all: float,
    ) -> float:
        """Compute the coarse deadtime confidence from the spec baseline."""
        cycle_factor = min(1.0, accepted_cycle_count / 20.0)
        separation_factor = min(1.0, max(0.0, ratio - 1.0) / 1.0)
        return cycle_factor * separation_factor * consistency_all

    @staticmethod
    def _lock_reason(
        *,
        accepted_cycle_count: int,
        ratio: float,
        recent_best_count: int,
    ) -> str | None:
        """Return the explicit lock blocker when the coarse deadtime stays unlocked."""
        if accepted_cycle_count < LOCK_MIN_ACCEPTED_CYCLES:
            return "deadtime_insufficient_cycles"
        if ratio < LOCK_MIN_DOMINANCE_RATIO:
            return "deadtime_insufficient_separation"
        if recent_best_count < LOCK_RECENT_BEST_COUNT:
            return "deadtime_inconsistent_winner"
        return None
