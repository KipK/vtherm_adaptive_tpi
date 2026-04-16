"""Factory for the Adaptive TPI proportional algorithm plugin."""

from __future__ import annotations

from vtherm_api.interfaces import (
    InterfacePropAlgorithmFactory,
    InterfacePropAlgorithmHandler,
    InterfaceThermostatRuntime,
)

from .const import PROP_FUNCTION_ADAPTIVE_TPI
from .handler import AdaptiveTPIHandler


class AdaptiveTPIHandlerFactory(InterfacePropAlgorithmFactory):
    """Create Adaptive TPI handlers for VT runtime thermostats."""

    @property
    def name(self) -> str:
        """Return the Adaptive TPI proportional function identifier."""
        return PROP_FUNCTION_ADAPTIVE_TPI

    def create(
        self,
        thermostat: InterfaceThermostatRuntime,
    ) -> InterfacePropAlgorithmHandler:
        """Create a handler bound to the runtime thermostat."""
        return AdaptiveTPIHandler(thermostat)

