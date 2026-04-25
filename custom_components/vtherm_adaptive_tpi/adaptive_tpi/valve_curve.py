"""Valve actuator linearization for Adaptive TPI."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from math import isfinite
from typing import Any, Mapping, Protocol

from ..const import ACTUATOR_MODE_SWITCH, ACTUATOR_MODE_VALVE
from .estimator import A_MIN
from .valve_curve_learning import (
    MIN_BRANCH_POINTS_FOR_CONVERGENCE,
    MIN_OBSERVATIONS_FOR_CONVERGENCE,
    OBSERVATION_HISTORY,
    RESIDUAL_CONVERGENCE_THRESHOLD,
    ValveCurveLearningObservation,
    clamp_unit,
    estimate_two_slope_update,
)


@dataclass(slots=True, frozen=True)
class ValveCurveParams:
    """Parameters of the two-slope valve characteristic in percent units."""

    min_valve: float
    knee_demand: float
    knee_valve: float
    max_valve: float

    def __post_init__(self) -> None:
        """Reject parameters that would make the curve ambiguous."""
        if not (0.0 <= self.min_valve < self.knee_valve < self.max_valve <= 100.0):
            raise ValueError("invalid valve curve valve breakpoints")
        if not (0.0 < self.knee_demand < 100.0):
            raise ValueError("invalid valve curve demand breakpoint")


VALVE_CURVE_DEFAULTS = ValveCurveParams(
    min_valve=7.0,
    knee_demand=80.0,
    knee_valve=15.0,
    max_valve=100.0,
)


class ValveCurveProtocol(Protocol):
    """Common interface for actuator linearization curves."""

    @property
    def actuator_mode(self) -> str:
        """Return the actuator mode served by this curve."""

    @property
    def params(self) -> ValveCurveParams | None:
        """Return curve parameters when the actuator needs linearization."""

    def apply(self, demand_unit: float) -> float:
        """Convert model demand in [0, 1] to actuator command in [0, 1]."""

    def invert(self, actuator_unit: float) -> float:
        """Convert actuator feedback in [0, 1] to model demand in [0, 1]."""

    def set_params(self, params: ValveCurveParams) -> None:
        """Replace the current curve parameters."""

    @property
    def learning_enabled(self) -> bool:
        """Return True when online learning may update the curve."""

    @property
    def is_converged(self) -> bool:
        """Return True when the curve fit is considered stable."""

    @property
    def observations_accepted_count(self) -> int:
        """Return the number of accepted learning observations."""

    @property
    def observations_rejected_count(self) -> int:
        """Return the number of rejected learning observations."""

    @property
    def rejected_updates(self) -> int:
        """Return the number of rejected parameter updates."""

    @property
    def last_reason(self) -> str | None:
        """Return the latest learning decision reason."""

    def set_learning_enabled(self, enabled: bool) -> None:
        """Enable or disable online curve learning."""

    def observe(
        self,
        *,
        u_valve: float,
        dTdt: float | None,
        delta_out: float,
        a_hat: float,
        b_hat: float,
        b_converged: bool,
        mode_sign: int,
        timestamp: str | None = None,
    ) -> None:
        """Feed one completed cycle observation to the curve learner."""

    def reset_learning(self) -> None:
        """Reset learned observations while keeping current parameters."""

    def to_persisted_dict(self) -> dict[str, Any]:
        """Serialize the curve state."""

    def load_persisted_dict(self, data: Mapping[str, Any] | None) -> bool:
        """Restore the curve state, returning True when data was accepted."""


class IdentityValveCurve:
    """Identity mapping for linear switch-like actuators."""

    actuator_mode = ACTUATOR_MODE_SWITCH
    params = None
    learning_enabled = False
    is_converged = False
    observations_accepted_count = 0
    observations_rejected_count = 0
    rejected_updates = 0
    last_reason = None

    def apply(self, demand_unit: float) -> float:
        """Return the demand unchanged."""
        return clamp_unit(demand_unit)

    def invert(self, actuator_unit: float) -> float:
        """Return the actuator value unchanged."""
        return clamp_unit(actuator_unit)

    def set_params(self, params: ValveCurveParams) -> None:
        """Ignore valve parameters for switch actuators."""
        del params

    def set_learning_enabled(self, enabled: bool) -> None:
        """Ignore learning toggles for switch actuators."""
        del enabled

    def observe(
        self,
        *,
        u_valve: float,
        dTdt: float | None,
        delta_out: float,
        a_hat: float,
        b_hat: float,
        b_converged: bool,
        mode_sign: int,
        timestamp: str | None = None,
    ) -> None:
        """Ignore learning observations for switch actuators."""
        del u_valve, dTdt, delta_out, a_hat, b_hat, b_converged, mode_sign, timestamp

    def reset_learning(self) -> None:
        """Nothing to reset for switch actuators."""

    def to_persisted_dict(self) -> dict[str, Any]:
        """Serialize the identity curve."""
        return {"actuator_mode": self.actuator_mode}

    def load_persisted_dict(self, data: Mapping[str, Any] | None) -> bool:
        """Accept only matching switch-mode persisted data."""
        if not isinstance(data, Mapping):
            return False
        return data.get("actuator_mode") == self.actuator_mode


class TwoSlopeValveCurve:
    """Two-slope Hammerstein static nonlinearity for TRV-like actuators."""

    actuator_mode = ACTUATOR_MODE_VALVE

    def __init__(
        self,
        params: ValveCurveParams = VALVE_CURVE_DEFAULTS,
        *,
        learning_enabled: bool = True,
    ) -> None:
        """Initialize the curve with validated parameters."""
        self._params = params
        self._configured_params = params
        self._learning_enabled = learning_enabled
        self._observations: deque[ValveCurveLearningObservation] = deque(
            maxlen=OBSERVATION_HISTORY
        )
        self._observations_accepted_count = 0
        self._observations_rejected_count = 0
        self._rejected_updates = 0
        self._is_converged = False
        self.last_reason: str | None = None

    @property
    def params(self) -> ValveCurveParams:
        """Return current curve parameters."""
        return self._params

    @property
    def learning_enabled(self) -> bool:
        """Return True when online curve learning is active."""
        return self._learning_enabled

    @property
    def is_converged(self) -> bool:
        """Return True when the bounded fit is stable enough for diagnostics."""
        return self._is_converged

    @property
    def observations_accepted_count(self) -> int:
        """Return the number of accepted learning observations."""
        return self._observations_accepted_count

    @property
    def observations_rejected_count(self) -> int:
        """Return the number of rejected learning observations."""
        return self._observations_rejected_count

    @property
    def rejected_updates(self) -> int:
        """Return the number of rejected parameter updates."""
        return self._rejected_updates

    def apply(self, demand_unit: float) -> float:
        """Map linear model demand to valve position."""
        demand = clamp_unit(demand_unit) * 100.0
        p = self._params
        if demand <= 0.0:
            return 0.0
        if demand <= p.knee_demand:
            valve = p.min_valve + (demand / p.knee_demand) * (
                p.knee_valve - p.min_valve
            )
        else:
            valve = p.knee_valve + (
                (demand - p.knee_demand) / (100.0 - p.knee_demand)
            ) * (p.max_valve - p.knee_valve)
        return clamp_unit(valve / 100.0)

    def invert(self, actuator_unit: float) -> float:
        """Map valve position to equivalent linear model demand."""
        valve = clamp_unit(actuator_unit) * 100.0
        p = self._params
        if valve < p.min_valve:
            return 0.0
        if valve <= p.knee_valve:
            demand = (valve - p.min_valve) * p.knee_demand / (
                p.knee_valve - p.min_valve
            )
        else:
            demand = p.knee_demand + (valve - p.knee_valve) * (
                100.0 - p.knee_demand
            ) / (p.max_valve - p.knee_valve)
        return clamp_unit(demand / 100.0)

    def set_params(self, params: ValveCurveParams) -> None:
        """Replace current validated parameters."""
        self._params = params

    def set_configured_params(self, params: ValveCurveParams) -> None:
        """Store the configured baseline parameters used for invalidation."""
        self._configured_params = params

    def set_learning_enabled(self, enabled: bool) -> None:
        """Enable or disable online curve learning."""
        self._learning_enabled = bool(enabled)

    def observe(
        self,
        *,
        u_valve: float,
        dTdt: float | None,
        delta_out: float,
        a_hat: float,
        b_hat: float,
        b_converged: bool,
        mode_sign: int,
        timestamp: str | None = None,
    ) -> None:
        """Learn the valve curve from one accepted cycle-aligned observation."""
        if not self._learning_enabled:
            self.last_reason = "learning_disabled"
            return
        if not b_converged:
            self._reject_observation("b_not_converged")
            return
        if dTdt is None or not isfinite(dTdt):
            self._reject_observation("missing_slope")
            return
        if not isfinite(delta_out):
            self._reject_observation("invalid_delta_out")
            return
        if abs(delta_out) < 1.0:
            self._reject_observation("insufficient_outdoor_delta")
            return
        valve_unit = clamp_unit(u_valve)
        if valve_unit * 100.0 < self._params.min_valve:
            self._reject_observation("below_deadband")
            return
        if not isfinite(a_hat) or a_hat <= A_MIN * 1.5:
            self._reject_observation("a_hat_too_low")
            return

        power_proxy = mode_sign * (float(dTdt) + (float(b_hat) * float(delta_out)))
        u_linear_equiv = clamp_unit(power_proxy / float(a_hat))
        if u_linear_equiv <= 0.0:
            self._reject_observation("non_positive_power_proxy")
            return

        self._observations.append(
            ValveCurveLearningObservation(
                u_linear_equiv=u_linear_equiv,
                u_valve=valve_unit,
                timestamp=timestamp,
            )
        )
        self._observations_accepted_count += 1
        self.last_reason = "sample_accepted"
        estimate = estimate_two_slope_update(
            tuple(self._observations),
            current_min_valve=self._params.min_valve,
            current_knee_demand=self._params.knee_demand,
            current_knee_valve=self._params.knee_valve,
            current_max_valve=self._params.max_valve,
        )
        if estimate is None:
            self._refresh_convergence()
            return
        try:
            self._params = ValveCurveParams(
                min_valve=estimate.min_valve,
                knee_demand=estimate.knee_demand,
                knee_valve=estimate.knee_valve,
                max_valve=estimate.max_valve,
            )
        except ValueError:
            self._rejected_updates += 1
            self.last_reason = "invalid_curve_update"
            self._refresh_convergence()
            return
        self._is_converged = (
            len(self._observations) >= MIN_OBSERVATIONS_FOR_CONVERGENCE
            and estimate.residual_dispersion < RESIDUAL_CONVERGENCE_THRESHOLD
            and estimate.low_branch_points >= MIN_BRANCH_POINTS_FOR_CONVERGENCE
            and estimate.high_branch_points >= MIN_BRANCH_POINTS_FOR_CONVERGENCE
        )
        self.last_reason = "sample_accepted"

    def reset_learning(self) -> None:
        """Clear the bounded observation history and counters."""
        self._observations.clear()
        self._observations_accepted_count = 0
        self._observations_rejected_count = 0
        self._rejected_updates = 0
        self._is_converged = False
        self.last_reason = None

    def _reject_observation(self, reason: str) -> None:
        """Record one rejected learning observation."""
        self._observations_rejected_count += 1
        self.last_reason = reason

    def _refresh_convergence(self) -> None:
        """Reset convergence when the bounded sample set is still too small."""
        self._is_converged = False

    def to_persisted_dict(self) -> dict[str, Any]:
        """Serialize curve parameters."""
        return {
            "actuator_mode": self.actuator_mode,
            "params": asdict(self._params),
            "learning_enabled": self._learning_enabled,
            "configured_params": asdict(self._configured_params),
            "observations": [
                {
                    "u_linear_equiv": observation.u_linear_equiv,
                    "u_valve": observation.u_valve,
                    "timestamp": observation.timestamp,
                }
                for observation in self._observations
            ],
            "observations_accepted_count": self._observations_accepted_count,
            "observations_rejected_count": self._observations_rejected_count,
            "rejected_updates": self._rejected_updates,
            "is_converged": self._is_converged,
            "last_reason": self.last_reason,
        }

    def load_persisted_dict(self, data: Mapping[str, Any] | None) -> bool:
        """Restore curve parameters from persisted state."""
        if not isinstance(data, Mapping):
            return False
        if data.get("actuator_mode") != self.actuator_mode:
            self.last_reason = "actuator_mode_changed"
            return False
        raw_params = data.get("params")
        if not isinstance(raw_params, Mapping):
            return False
        try:
            self._params = ValveCurveParams(
                min_valve=float(raw_params["min_valve"]),
                knee_demand=float(raw_params["knee_demand"]),
                knee_valve=float(raw_params["knee_valve"]),
                max_valve=float(raw_params["max_valve"]),
            )
        except (KeyError, TypeError, ValueError):
            return False
        if isinstance(data.get("learning_enabled"), bool):
            self._learning_enabled = data["learning_enabled"]
        raw_configured_params = data.get("configured_params")
        if isinstance(raw_configured_params, Mapping):
            try:
                self._configured_params = ValveCurveParams(
                    min_valve=float(raw_configured_params["min_valve"]),
                    knee_demand=float(raw_configured_params["knee_demand"]),
                    knee_valve=float(raw_configured_params["knee_valve"]),
                    max_valve=float(raw_configured_params["max_valve"]),
                )
            except (KeyError, TypeError, ValueError):
                self._configured_params = self._params
        raw_observations = data.get("observations")
        self._observations.clear()
        if isinstance(raw_observations, list):
            for raw_observation in raw_observations[-OBSERVATION_HISTORY:]:
                if not isinstance(raw_observation, Mapping):
                    continue
                try:
                    self._observations.append(
                        ValveCurveLearningObservation(
                            u_linear_equiv=clamp_unit(float(raw_observation["u_linear_equiv"])),
                            u_valve=clamp_unit(float(raw_observation["u_valve"])),
                            timestamp=(
                                raw_observation.get("timestamp")
                                if isinstance(raw_observation.get("timestamp"), str)
                                else None
                            ),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
        self._observations_accepted_count = int(data.get("observations_accepted_count", 0) or 0)
        self._observations_rejected_count = int(data.get("observations_rejected_count", 0) or 0)
        self._rejected_updates = int(data.get("rejected_updates", 0) or 0)
        self._is_converged = bool(data.get("is_converged", False))
        if isinstance(data.get("last_reason"), str):
            self.last_reason = data["last_reason"]
        return True


def build_valve_curve(
    actuator_mode: str,
    persisted: Mapping[str, Any] | None = None,
    *,
    params: ValveCurveParams | None = None,
    compensation_enabled: bool = True,
    learning_enabled: bool = True,
) -> ValveCurveProtocol:
    """Build the linearization curve for one actuator mode."""
    curve: ValveCurveProtocol
    if actuator_mode == ACTUATOR_MODE_VALVE and compensation_enabled:
        curve = TwoSlopeValveCurve(
            params=params or VALVE_CURVE_DEFAULTS,
            learning_enabled=learning_enabled,
        )
    else:
        curve = IdentityValveCurve()
    curve.load_persisted_dict(persisted)
    return curve
