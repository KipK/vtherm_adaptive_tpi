"""Learning helpers for the Adaptive TPI valve linearization curve."""

from __future__ import annotations

from dataclasses import dataclass
from math import floor, isfinite
from statistics import median
from typing import Sequence


OBSERVATION_HISTORY = 30
MIN_OBSERVATIONS_FOR_UPDATE = 5
MIN_OBSERVATIONS_FOR_CONVERGENCE = 15
MIN_BRANCH_POINTS_FOR_CONVERGENCE = 3
MAX_PARAM_STEP = 2.0
MIN_ACTIVE_DEMAND = 1e-3
MIN_MAX_VALVE_FOR_SATURATION = 50.0
MIN_DEMAND_FOR_SATURATION = 95.0
RESIDUAL_CONVERGENCE_THRESHOLD = 0.15


@dataclass(slots=True, frozen=True)
class ValveCurveLearningObservation:
    """One bounded learning sample for the valve characteristic."""

    u_linear_equiv: float
    u_valve: float
    timestamp: str | None = None


@dataclass(slots=True, frozen=True)
class ValveCurveLearningEstimate:
    """Candidate parameter update built from the bounded observation history."""

    min_valve: float
    knee_demand: float
    knee_valve: float
    max_valve: float
    residual_dispersion: float
    low_branch_points: int
    high_branch_points: int


def clamp_percent(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
    """Clamp one percentage-like value to an inclusive range."""
    return min(max(float(value), lower), upper)


def clamp_unit(value: float) -> float:
    """Clamp one unit value to [0, 1]."""
    return clamp_percent(value, 0.0, 1.0)


def quantile(values: Sequence[float], q: float) -> float:
    """Return a simple bounded quantile using nearest-rank indexing."""
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    index = int(floor(max(0.0, min(1.0, q)) * (len(ordered) - 1)))
    return ordered[index]


def weighted_median(values: Sequence[float], weights: Sequence[float]) -> float:
    """Return the weighted median of a finite sample."""
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    pairs = sorted(
        (
            (float(value), max(0.0, float(weight)))
            for value, weight in zip(values, weights, strict=False)
            if isfinite(float(value))
        ),
        key=lambda pair: pair[0],
    )
    if not pairs:
        return 0.0
    total_weight = sum(weight for _, weight in pairs)
    if total_weight <= 0.0:
        return float(median(value for value, _ in pairs))
    threshold = total_weight / 2.0
    cumulative = 0.0
    for value, weight in pairs:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return pairs[-1][0]


def rate_limit(target: float, current: float) -> float:
    """Bound one parameter move to the configured per-cycle maximum."""
    delta = float(target) - float(current)
    if delta > MAX_PARAM_STEP:
        return float(current) + MAX_PARAM_STEP
    if delta < -MAX_PARAM_STEP:
        return float(current) - MAX_PARAM_STEP
    return float(target)


def evaluate_two_slope_curve(
    *,
    demand_percent: float,
    min_valve: float,
    knee_demand: float,
    knee_valve: float,
    max_valve: float,
) -> float:
    """Evaluate the two-slope curve in percent units."""
    demand = clamp_percent(demand_percent)
    if demand < min_valve:
        return 0.0
    if demand < knee_demand:
        return min_valve + (demand / knee_demand) * (knee_valve - min_valve)
    return knee_valve + ((demand - knee_demand) / (100.0 - knee_demand)) * (
        max_valve - knee_valve
    )


def residual_dispersion(
    observations: Sequence[ValveCurveLearningObservation],
    *,
    min_valve: float,
    knee_demand: float,
    knee_valve: float,
    max_valve: float,
) -> float:
    """Return a robust relative MAD of valve residuals."""
    if not observations:
        return 1.0
    residuals = [
        observation.u_valve * 100.0
        - evaluate_two_slope_curve(
            demand_percent=observation.u_linear_equiv * 100.0,
            min_valve=min_valve,
            knee_demand=knee_demand,
            knee_valve=knee_valve,
            max_valve=max_valve,
        )
        for observation in observations
    ]
    center = median(residuals)
    mad = median(abs(residual - center) for residual in residuals)
    scale = max(1.0, median(abs(observation.u_valve * 100.0) for observation in observations))
    return mad / scale


def estimate_two_slope_update(
    observations: Sequence[ValveCurveLearningObservation],
    *,
    current_min_valve: float,
    current_knee_demand: float,
    current_knee_valve: float,
    current_max_valve: float,
) -> ValveCurveLearningEstimate | None:
    """Estimate one robust two-slope parameter update from bounded samples."""
    finite_observations = [
        observation
        for observation in observations
        if isfinite(observation.u_linear_equiv)
        and isfinite(observation.u_valve)
        and observation.u_linear_equiv >= 0.0
        and observation.u_valve >= 0.0
    ]
    if len(finite_observations) < MIN_OBSERVATIONS_FOR_UPDATE:
        return None

    active_valves = [
        clamp_percent(observation.u_valve * 100.0)
        for observation in finite_observations
        if observation.u_linear_equiv > MIN_ACTIVE_DEMAND
    ]
    if not active_valves:
        return None

    base_min_valve = clamp_percent(
        quantile(active_valves, 0.10),
        1.0,
        max(1.0, current_knee_valve - 1.0),
    )

    saturation_valves = [
        clamp_percent(observation.u_valve * 100.0)
        for observation in finite_observations
        if observation.u_linear_equiv * 100.0 >= MIN_DEMAND_FOR_SATURATION
        and observation.u_valve * 100.0 >= MIN_MAX_VALVE_FOR_SATURATION
    ]
    target_max_valve = (
        clamp_percent(quantile(saturation_valves, 0.90), base_min_valve + 2.0, 100.0)
        if saturation_valves
        else current_max_valve
    )

    best_candidate: ValveCurveLearningEstimate | None = None
    best_score: float | None = None
    candidate_weights = [1.0 for _ in finite_observations]
    candidate_min_start = int(floor(base_min_valve)) + 1
    candidate_min_end = int(min(50.0, floor(target_max_valve - 2.0)))
    for candidate_knee_valve in range(candidate_min_start, candidate_min_end + 1):
        for candidate_knee_demand in range(50, 96):
            score = weighted_median(
                [
                    abs(
                        observation.u_valve * 100.0
                        - evaluate_two_slope_curve(
                            demand_percent=observation.u_linear_equiv * 100.0,
                            min_valve=base_min_valve,
                            knee_demand=float(candidate_knee_demand),
                            knee_valve=float(candidate_knee_valve),
                            max_valve=target_max_valve,
                        )
                    )
                    for observation in finite_observations
                ],
                candidate_weights,
            )
            if best_score is not None and score >= best_score:
                continue
            best_score = score
            best_candidate = ValveCurveLearningEstimate(
                min_valve=base_min_valve,
                knee_demand=float(candidate_knee_demand),
                knee_valve=float(candidate_knee_valve),
                max_valve=target_max_valve,
                residual_dispersion=0.0,
                low_branch_points=0,
                high_branch_points=0,
            )

    if best_candidate is None:
        return None

    limited_min_valve = clamp_percent(
        rate_limit(best_candidate.min_valve, current_min_valve),
        1.0,
        max(1.0, current_knee_valve - 1.0),
    )
    limited_knee_valve = clamp_percent(
        rate_limit(best_candidate.knee_valve, current_knee_valve),
        limited_min_valve + 1.0,
        max(limited_min_valve + 1.0, best_candidate.max_valve - 1.0),
    )
    limited_knee_demand = clamp_percent(
        rate_limit(best_candidate.knee_demand, current_knee_demand),
        1.0,
        99.0,
    )
    limited_max_valve = clamp_percent(
        rate_limit(best_candidate.max_valve, current_max_valve),
        limited_knee_valve + 1.0,
        100.0,
    )
    low_branch_points = sum(
        1
        for observation in finite_observations
        if observation.u_valve * 100.0 < limited_knee_valve
    )
    high_branch_points = sum(
        1
        for observation in finite_observations
        if observation.u_valve * 100.0 >= limited_knee_valve
    )
    return ValveCurveLearningEstimate(
        min_valve=limited_min_valve,
        knee_demand=limited_knee_demand,
        knee_valve=limited_knee_valve,
        max_valve=limited_max_valve,
        residual_dispersion=residual_dispersion(
            finite_observations,
            min_valve=limited_min_valve,
            knee_demand=limited_knee_demand,
            knee_valve=limited_knee_valve,
            max_valve=limited_max_valve,
        ),
        low_branch_points=low_branch_points,
        high_branch_points=high_branch_points,
    )
