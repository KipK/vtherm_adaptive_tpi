"""Parameter estimation primitives for Adaptive TPI."""

from __future__ import annotations

from dataclasses import dataclass
from math import exp, floor

from .deadtime import DeadtimeObservation

MU0 = 0.08
EPS0 = 1e-4
A_MIN = 1e-3
A_MAX = 2.0
B_MIN = 0.0
B_MAX = 0.5
ALPHA_C = 0.05
E_SCALE = 0.10

U_MIN_ID = 0.15
U_MAX_ID = 0.60
D_OUT_MIN = 1.0
D_OUT_MAX = 5.0
D_IN_MIN = 0.2
D_IN_MAX = 1.0


def _clamp(value: float, lower: float, upper: float) -> float:
    """Clamp a value to an inclusive range."""
    return min(max(value, lower), upper)


@dataclass(slots=True)
class EstimatorSample:
    """Single-cycle sample aligned for the lightweight v1 estimator."""

    y: float
    u_del: float
    loss_input: float
    c_nd: float
    i_a: float
    i_b: float
    i_global: float


@dataclass(slots=True)
class EstimatorUpdate:
    """Expose the estimator update output for runtime wiring."""

    a_hat: float
    b_hat: float
    c_a: float
    c_b: float
    i_a: float
    i_b: float
    residual: float
    updated: bool


def build_estimator_sample(
    observations: tuple[DeadtimeObservation, ...],
    *,
    nd_hat: float,
    c_nd: float,
) -> EstimatorSample | None:
    """Build the latest aligned estimator sample from accepted observations."""
    if len(observations) < 2:
        return None

    current_index = len(observations) - 2
    delayed_index = current_index - floor(max(nd_hat, 0.0))
    if delayed_index < 0:
        return None

    current = observations[current_index]
    nxt = observations[current_index + 1]
    delayed = observations[delayed_index]

    y = nxt.tin - current.tin
    loss_input = -(current.tin - current.tout)
    i_a, i_b, i_global = compute_excitation_scores(
        u_del=delayed.applied_power,
        tin=current.tin,
        tout=current.tout,
        target_temp=current.target_temp,
    )
    return EstimatorSample(
        y=y,
        u_del=delayed.applied_power,
        loss_input=loss_input,
        c_nd=c_nd,
        i_a=i_a,
        i_b=i_b,
        i_global=i_global,
    )


def compute_excitation_scores(
    *,
    u_del: float,
    tin: float,
    tout: float,
    target_temp: float,
) -> tuple[float, float, float]:
    """Compute the v1 excitation scores used by the estimator."""
    i_a = _clamp((u_del - U_MIN_ID) / (U_MAX_ID - U_MIN_ID), 0.0, 1.0)
    delta_out = abs(tin - tout)
    i_b = _clamp((delta_out - D_OUT_MIN) / (D_OUT_MAX - D_OUT_MIN), 0.0, 1.0)
    delta_in = abs(target_temp - tin)
    i_e = _clamp((delta_in - D_IN_MIN) / (D_IN_MAX - D_IN_MIN), 0.0, 1.0)
    return i_a, i_b, min(i_a, i_b, i_e)


class ParameterEstimator:
    """Lightweight constrained gradient estimator for `a_hat` and `b_hat`."""

    def __init__(self) -> None:
        """Initialize the estimator state."""
        self.a_hat = A_MIN
        self.b_hat = B_MIN
        self.c_a = 0.0
        self.c_b = 0.0

    def reset(self) -> None:
        """Reset estimator state."""
        self.a_hat = A_MIN
        self.b_hat = B_MIN
        self.c_a = 0.0
        self.c_b = 0.0

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

    def update(self, sample: EstimatorSample | None) -> EstimatorUpdate:
        """Run one normalized constrained gradient update."""
        if sample is None:
            return EstimatorUpdate(
                a_hat=self.a_hat,
                b_hat=self.b_hat,
                c_a=self.c_a,
                c_b=self.c_b,
                i_a=0.0,
                i_b=0.0,
                residual=0.0,
                updated=False,
            )

        phi_norm_sq = (sample.u_del * sample.u_del) + (sample.loss_input * sample.loss_input)
        prediction = (sample.u_del * self.a_hat) + (sample.loss_input * self.b_hat)
        residual = sample.y - prediction
        mu = MU0 * sample.c_nd * sample.i_global

        if mu > 0.0:
            step = mu * residual / (EPS0 + phi_norm_sq)
            self.a_hat = _clamp(self.a_hat + (step * sample.u_del), A_MIN, A_MAX)
            self.b_hat = _clamp(self.b_hat + (step * sample.loss_input), B_MIN, B_MAX)

        q = exp(-abs(residual) / E_SCALE)
        self.c_a = _clamp(
            ((1.0 - ALPHA_C) * self.c_a) + (ALPHA_C * q * sample.i_a * sample.c_nd),
            0.0,
            1.0,
        )
        self.c_b = _clamp(
            ((1.0 - ALPHA_C) * self.c_b) + (ALPHA_C * q * sample.i_b * sample.c_nd),
            0.0,
            1.0,
        )

        return EstimatorUpdate(
            a_hat=self.a_hat,
            b_hat=self.b_hat,
            c_a=self.c_a,
            c_b=self.c_b,
            i_a=sample.i_a,
            i_b=sample.i_b,
            residual=residual,
            updated=mu > 0.0,
        )
