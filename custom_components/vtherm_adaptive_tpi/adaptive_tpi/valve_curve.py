"""Valve actuator linearization for Adaptive TPI."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping, Protocol

from ..const import ACTUATOR_MODE_SWITCH, ACTUATOR_MODE_VALVE


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

    def to_persisted_dict(self) -> dict[str, Any]:
        """Serialize the curve state."""

    def load_persisted_dict(self, data: Mapping[str, Any] | None) -> bool:
        """Restore the curve state, returning True when data was accepted."""


def _clamp_unit(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


class IdentityValveCurve:
    """Identity mapping for linear switch-like actuators."""

    actuator_mode = ACTUATOR_MODE_SWITCH
    params = None

    def apply(self, demand_unit: float) -> float:
        """Return the demand unchanged."""
        return _clamp_unit(demand_unit)

    def invert(self, actuator_unit: float) -> float:
        """Return the actuator value unchanged."""
        return _clamp_unit(actuator_unit)

    def set_params(self, params: ValveCurveParams) -> None:
        """Ignore valve parameters for switch actuators."""
        del params

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

    def __init__(self, params: ValveCurveParams = VALVE_CURVE_DEFAULTS) -> None:
        """Initialize the curve with validated parameters."""
        self._params = params
        self.last_reason: str | None = None

    @property
    def params(self) -> ValveCurveParams:
        """Return current curve parameters."""
        return self._params

    def apply(self, demand_unit: float) -> float:
        """Map linear model demand to valve position."""
        demand = _clamp_unit(demand_unit) * 100.0
        p = self._params
        if demand < p.min_valve:
            return 0.0
        if demand < p.knee_demand:
            valve = p.min_valve + (demand / p.knee_demand) * (
                p.knee_valve - p.min_valve
            )
        else:
            valve = p.knee_valve + (
                (demand - p.knee_demand) / (100.0 - p.knee_demand)
            ) * (p.max_valve - p.knee_valve)
        return _clamp_unit(valve / 100.0)

    def invert(self, actuator_unit: float) -> float:
        """Map valve position to equivalent linear model demand."""
        valve = _clamp_unit(actuator_unit) * 100.0
        p = self._params
        if valve <= 0.0:
            return 0.0
        first_reachable = p.min_valve + (p.min_valve / p.knee_demand) * (
            p.knee_valve - p.min_valve
        )
        if valve < first_reachable:
            return 0.0
        if valve <= p.knee_valve:
            demand = (valve - p.min_valve) * p.knee_demand / (
                p.knee_valve - p.min_valve
            )
        else:
            demand = p.knee_demand + (valve - p.knee_valve) * (
                100.0 - p.knee_demand
            ) / (p.max_valve - p.knee_valve)
        return _clamp_unit(demand / 100.0)

    def set_params(self, params: ValveCurveParams) -> None:
        """Replace current validated parameters."""
        self._params = params

    def to_persisted_dict(self) -> dict[str, Any]:
        """Serialize curve parameters."""
        return {
            "actuator_mode": self.actuator_mode,
            "params": asdict(self._params),
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
        return True


def build_valve_curve(
    actuator_mode: str,
    persisted: Mapping[str, Any] | None = None,
) -> ValveCurveProtocol:
    """Build the linearization curve for one actuator mode."""
    curve: ValveCurveProtocol
    if actuator_mode == ACTUATOR_MODE_VALVE:
        curve = TwoSlopeValveCurve()
    else:
        curve = IdentityValveCurve()
    curve.load_persisted_dict(persisted)
    return curve
