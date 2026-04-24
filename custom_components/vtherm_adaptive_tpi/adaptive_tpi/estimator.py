"""Parameter estimation primitives for Adaptive TPI."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from statistics import median
from typing import Any, Mapping

A_MIN = 1e-3
A_MAX = 2.0
B_MIN = 0.0
B_MAX = 0.5
WINDOW_HISTORY = 12
B_CONVERGENCE_MIN_SAMPLES = 3
B_CONVERGENCE_MIN_CONFIDENCE = 0.55
MIN_B_DELTA_OUT = 0.5
MIN_A_DELTA_OUT = 1.0
MIN_SETPOINT_ERROR = 0.2
MAX_OFF_U_EFF = 0.15
MIN_ON_U_EFF = 0.25
MAD_OUTLIER_MIN_SAMPLES = 5
MAD_OUTLIER_ROBUST_Z_THRESHOLD = 4.5
MAD_NORMALIZATION_FACTOR = 1.4826
OUTLIER_REGIME_CONFIRMATION_COUNT = 3
OUTLIER_REGIME_MAX_RELATIVE_DISPERSION = 0.20
OUTLIER_EPSILON = 1e-9


def _clamp(value: float, lower: float, upper: float) -> float:
    """Clamp a value to an inclusive range."""
    return min(max(value, lower), upper)


@dataclass(slots=True)
class BSample:
    """Window-based thermal loss sample."""

    dTdt: float
    delta_out: float
    setpoint_error: float
    u_eff: float
    allow_near_setpoint_b: bool = False


@dataclass(slots=True)
class ASample:
    """Window-based heating authority sample."""

    dTdt: float
    delta_out: float
    setpoint_error: float
    u_eff: float


@dataclass(slots=True)
class EstimatorUpdate:
    """Expose the estimator state after one routed update."""

    a_hat: float
    b_hat: float
    c_a: float
    c_b: float
    i_a: float
    i_b: float
    a_updated: bool
    b_updated: bool
    updated: bool
    b_converged: bool
    a_samples_count: int
    b_samples_count: int
    a_last_reason: str
    b_last_reason: str
    a_dispersion: float
    b_dispersion: float


class _RobustScalarEstimator:
    """Small robust scalar estimator based on bounded rolling medians."""

    def __init__(self, *, lower: float, upper: float) -> None:
        self._lower = lower
        self._upper = upper
        self._samples: deque[float] = deque(maxlen=WINDOW_HISTORY)
        self._outlier_candidates: deque[float] = deque(maxlen=OUTLIER_REGIME_CONFIRMATION_COUNT)
        self.last_reason = "not_initialized"

    def reset(self) -> None:
        self._samples.clear()
        self._outlier_candidates.clear()
        self.last_reason = "not_initialized"

    def restore(self, estimate: float, confidence: float) -> None:
        del confidence
        self._samples.clear()
        self._outlier_candidates.clear()
        self.last_reason = "restored"
        bounded = _clamp(estimate, self._lower, self._upper)
        if bounded > self._lower:
            self._samples.append(bounded)

    def to_persisted_dict(self) -> dict[str, Any]:
        """Serialize the bounded sample history required for warm starts."""
        return {
            "samples": list(self._samples),
            "last_reason": self.last_reason,
        }

    def load_persisted_dict(self, data: Mapping[str, Any] | None) -> bool:
        """Restore the bounded sample history from persistence."""
        if not isinstance(data, Mapping):
            return False

        raw_samples = data.get("samples")
        if not isinstance(raw_samples, list):
            return False

        cleaned_samples: list[float] = []
        for raw_sample in raw_samples[-WINDOW_HISTORY:]:
            if isinstance(raw_sample, bool):
                continue
            try:
                cleaned_samples.append(_clamp(float(raw_sample), self._lower, self._upper))
            except (TypeError, ValueError):
                continue

        self._samples.clear()
        self._samples.extend(cleaned_samples)
        self._outlier_candidates.clear()

        last_reason = data.get("last_reason")
        self.last_reason = last_reason if isinstance(last_reason, str) and last_reason else "restored"
        return True

    @property
    def samples_count(self) -> int:
        return len(self._samples)

    @property
    def dispersion(self) -> float:
        if len(self._samples) < 2:
            return 1.0 if self._samples else 0.0
        center = median(self._samples)
        if abs(center) < 1e-9:
            return 1.0
        abs_deviations = [abs(sample - center) for sample in self._samples]
        return min(1.0, median(abs_deviations) / abs(center))

    @property
    def estimate(self) -> float:
        if not self._samples:
            return self._lower
        return _clamp(float(median(self._samples)), self._lower, self._upper)

    @property
    def confidence(self) -> float:
        if not self._samples:
            return 0.0
        count_score = min(1.0, len(self._samples) / 6.0)
        stability_score = max(0.0, 1.0 - self.dispersion)
        return _clamp(count_score * stability_score, 0.0, 1.0)

    def push(self, measurement: float) -> None:
        self._samples.append(_clamp(measurement, self._lower, self._upper))
        self.last_reason = "sample_accepted"

    def is_outlier(self, measurement: float) -> bool:
        if len(self._samples) < MAD_OUTLIER_MIN_SAMPLES:
            return False
        center = median(self._samples)
        mad = median([abs(s - center) for s in self._samples])
        if mad <= OUTLIER_EPSILON:
            return False
        robust_z = abs(measurement - center) / (MAD_NORMALIZATION_FACTOR * mad)
        return robust_z > MAD_OUTLIER_ROBUST_Z_THRESHOLD

    def record_outlier_candidate(self, measurement: float) -> bool:
        self._outlier_candidates.append(_clamp(measurement, self._lower, self._upper))
        if len(self._outlier_candidates) < OUTLIER_REGIME_CONFIRMATION_COUNT:
            return False
        candidates = list(self._outlier_candidates)
        center = median(candidates)
        if abs(center) <= OUTLIER_EPSILON:
            return max(abs(c - center) for c in candidates) <= OUTLIER_EPSILON
        max_deviation = max(abs(c - center) for c in candidates)
        return max_deviation / abs(center) <= OUTLIER_REGIME_MAX_RELATIVE_DISPERSION

    def confirm_outlier_regime(self) -> None:
        self._samples.clear()
        self._samples.extend(self._outlier_candidates)
        self._outlier_candidates.clear()

    def clear_outlier_candidates(self) -> None:
        self._outlier_candidates.clear()


class ParameterEstimator:
    """Decoupled routed estimator for `b_hat` and `a_hat`."""

    def __init__(self) -> None:
        self._a_estimator = _RobustScalarEstimator(lower=A_MIN, upper=A_MAX)
        self._b_estimator = _RobustScalarEstimator(lower=B_MIN, upper=B_MAX)
        self.a_hat = A_MIN
        self.b_hat = B_MIN
        self.c_a = 0.0
        self.c_b = 0.0
        self.b_converged = False


    def reset(self) -> None:
        """Reset estimator state."""
        self._a_estimator.reset()
        self._b_estimator.reset()
        self.a_hat = A_MIN
        self.b_hat = B_MIN
        self.c_a = 0.0
        self.c_b = 0.0
        self.b_converged = False

    def restore(
        self,
        *,
        a_hat: float,
        b_hat: float,
        c_a: float,
        c_b: float,
    ) -> None:
        """Restore estimator state from the runtime snapshot."""
        self.a_hat = _clamp(a_hat, A_MIN, A_MAX)
        self.b_hat = _clamp(b_hat, B_MIN, B_MAX)
        self.c_a = _clamp(c_a, 0.0, 1.0)
        self.c_b = _clamp(c_b, 0.0, 1.0)
        self._a_estimator.restore(self.a_hat, self.c_a)
        self._b_estimator.restore(self.b_hat, self.c_b)
        self.b_converged = self._compute_b_converged()

    def to_persisted_dict(self) -> dict[str, Any]:
        """Serialize estimator internals required for warm starts."""
        return {
            "a_estimator": self._a_estimator.to_persisted_dict(),
            "b_estimator": self._b_estimator.to_persisted_dict(),
        }

    def load_persisted_dict(self, data: Mapping[str, Any] | None) -> bool:
        """Restore estimator internals from persistence when available."""
        if not isinstance(data, Mapping):
            return False

        a_restored = self._a_estimator.load_persisted_dict(data.get("a_estimator"))
        b_restored = self._b_estimator.load_persisted_dict(data.get("b_estimator"))
        if not a_restored or not b_restored:
            return False

        self.a_hat = self._a_estimator.estimate if self._a_estimator.samples_count else A_MIN
        self.b_hat = self._b_estimator.estimate if self._b_estimator.samples_count else B_MIN
        self.c_a = self._a_estimator.confidence
        self.c_b = self._b_estimator.confidence
        self.b_converged = self._compute_b_converged()
        return True

    def update_b(self, sample: BSample | None, reason: str = "b_sample_missing") -> EstimatorUpdate:
        """Update the thermal loss estimator only."""
        updated = False
        if sample is None:
            self._b_estimator.last_reason = reason
            return self._snapshot(i_a=0.0, i_b=0.0, a_updated=False, b_updated=False)

        if abs(sample.delta_out) < MIN_B_DELTA_OUT:
            self._b_estimator.last_reason = "b_delta_out_too_small"
            return self._snapshot(i_a=0.0, i_b=0.0, a_updated=False, b_updated=False)
        if (
            sample.setpoint_error < MIN_SETPOINT_ERROR
            and not sample.allow_near_setpoint_b
        ):
            self._b_estimator.last_reason = "b_setpoint_error_too_small"
            return self._snapshot(i_a=0.0, i_b=0.0, a_updated=False, b_updated=False)
        if sample.u_eff > MAX_OFF_U_EFF:
            self._b_estimator.last_reason = "b_window_not_quasi_off"
            return self._snapshot(i_a=0.0, i_b=0.0, a_updated=False, b_updated=False)

        measurement = -(sample.dTdt / sample.delta_out)
        if measurement < B_MIN:
            self._b_estimator.last_reason = "b_measurement_unphysical"
            return self._snapshot(i_a=0.0, i_b=1.0, a_updated=False, b_updated=False)

        if self._b_estimator.is_outlier(measurement):
            confirmed = self._b_estimator.record_outlier_candidate(measurement)
            if not confirmed:
                self._b_estimator.last_reason = "b_measurement_outlier_mad"
                return self._snapshot(i_a=0.0, i_b=1.0, a_updated=False, b_updated=False)
            self._b_estimator.confirm_outlier_regime()
            self._b_estimator.last_reason = "b_outlier_regime_confirmed"
            self.b_hat = self._b_estimator.estimate
            self.c_b = self._b_estimator.confidence
            self.b_converged = self._compute_b_converged()
            return self._snapshot(i_a=0.0, i_b=1.0, a_updated=False, b_updated=True)
        self._b_estimator.clear_outlier_candidates()

        self._b_estimator.push(measurement)
        self.b_hat = self._b_estimator.estimate
        self.c_b = self._b_estimator.confidence
        self.b_converged = self._compute_b_converged()
        updated = True
        return self._snapshot(i_a=0.0, i_b=1.0, a_updated=False, b_updated=updated)

    def seed_b_from_deadtime_proxy(
        self,
        measurement: float | None,
        *,
        reason: str = "b_seeded_from_deadtime",
    ) -> EstimatorUpdate:
        """Bootstrap `b` once from the deadtime-side proxy when no OFF sample exists yet."""
        if measurement is None:
            self._b_estimator.last_reason = "b_deadtime_proxy_missing"
            return self._snapshot(i_a=0.0, i_b=0.0, a_updated=False, b_updated=False)
        if self._b_estimator.samples_count > 0:
            self._b_estimator.last_reason = "b_deadtime_seed_skipped_existing_samples"
            return self._snapshot(i_a=0.0, i_b=0.0, a_updated=False, b_updated=False)

        self._b_estimator.push(measurement)
        self._b_estimator.last_reason = reason
        self.b_hat = self._b_estimator.estimate
        self.c_b = self._b_estimator.confidence
        self.b_converged = self._compute_b_converged()
        return self._snapshot(i_a=0.0, i_b=0.0, a_updated=False, b_updated=True)

    def update_a(
        self,
        sample: ASample | None,
        reason: str = "a_sample_missing",
        mode_sign: int = 1,
    ) -> EstimatorUpdate:
        """Update the actuator authority estimator only."""
        if sample is None:
            self._a_estimator.last_reason = reason
            return self._snapshot(i_a=0.0, i_b=0.0, a_updated=False, b_updated=False)

        if not self.b_converged:
            self._a_estimator.last_reason = "a_waiting_b_converged"
            return self._snapshot(i_a=0.0, i_b=0.0, a_updated=False, b_updated=False)
        if abs(sample.delta_out) < MIN_A_DELTA_OUT:
            self._a_estimator.last_reason = "a_delta_out_too_small"
            return self._snapshot(i_a=1.0, i_b=0.0, a_updated=False, b_updated=False)
        if sample.setpoint_error < MIN_SETPOINT_ERROR:
            self._a_estimator.last_reason = "a_setpoint_error_too_small"
            return self._snapshot(i_a=1.0, i_b=0.0, a_updated=False, b_updated=False)
        if sample.u_eff < MIN_ON_U_EFF:
            self._a_estimator.last_reason = "a_u_eff_too_small"
            return self._snapshot(i_a=1.0, i_b=0.0, a_updated=False, b_updated=False)

        # HEAT: dT/dt = a*u - b*delta_out  →  a = (dTdt + b*delta_out) / u
        # COOL: dT/dt = -a*u - b*delta_out →  a = -(dTdt + b*delta_out) / u
        measurement = mode_sign * (sample.dTdt + (self.b_hat * sample.delta_out)) / sample.u_eff
        if measurement < A_MIN:
            self._a_estimator.last_reason = "a_measurement_unphysical"
            return self._snapshot(i_a=1.0, i_b=0.0, a_updated=False, b_updated=False)

        if self._a_estimator.is_outlier(measurement):
            confirmed = self._a_estimator.record_outlier_candidate(measurement)
            if not confirmed:
                self._a_estimator.last_reason = "a_measurement_outlier_mad"
                return self._snapshot(i_a=1.0, i_b=0.0, a_updated=False, b_updated=False)
            self._a_estimator.confirm_outlier_regime()
            self._a_estimator.last_reason = "a_outlier_regime_confirmed"
            self.a_hat = self._a_estimator.estimate
            self.c_a = self._a_estimator.confidence
            return self._snapshot(i_a=1.0, i_b=0.0, a_updated=True, b_updated=False)
        self._a_estimator.clear_outlier_candidates()

        self._a_estimator.push(measurement)
        self.a_hat = self._a_estimator.estimate
        self.c_a = self._a_estimator.confidence
        return self._snapshot(i_a=1.0, i_b=0.0, a_updated=True, b_updated=False)

    def _compute_b_converged(self) -> bool:
        return (
            self._b_estimator.samples_count >= B_CONVERGENCE_MIN_SAMPLES
            and self.c_b >= B_CONVERGENCE_MIN_CONFIDENCE
        )

    def _snapshot(self, *, i_a: float, i_b: float, a_updated: bool, b_updated: bool) -> EstimatorUpdate:
        self.a_hat = self._a_estimator.estimate if self._a_estimator.samples_count else self.a_hat
        self.b_hat = self._b_estimator.estimate if self._b_estimator.samples_count else self.b_hat
        self.c_a = self._a_estimator.confidence if self._a_estimator.samples_count else self.c_a
        self.c_b = self._b_estimator.confidence if self._b_estimator.samples_count else self.c_b
        self.b_converged = self._compute_b_converged()
        return EstimatorUpdate(
            a_hat=self.a_hat,
            b_hat=self.b_hat,
            c_a=self.c_a,
            c_b=self.c_b,
            i_a=i_a,
            i_b=i_b,
            a_updated=a_updated,
            b_updated=b_updated,
            updated=(a_updated or b_updated),
            b_converged=self.b_converged,
            a_samples_count=self._a_estimator.samples_count,
            b_samples_count=self._b_estimator.samples_count,
            a_last_reason=self._a_estimator.last_reason,
            b_last_reason=self._b_estimator.last_reason,
            a_dispersion=self._a_estimator.dispersion,
            b_dispersion=self._b_estimator.dispersion,
        )
