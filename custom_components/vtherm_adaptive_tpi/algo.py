"""Runtime algorithm wrapper for Adaptive TPI."""

from __future__ import annotations

import logging
from typing import Any, Mapping

from .adaptive_tpi.controller import compute_on_percent
from .adaptive_tpi.diagnostics import build_diagnostics
from .adaptive_tpi.state import AdaptiveTPIState
from .adaptive_tpi.supervisor import AdaptiveTPISupervisor
from .const import DEFAULT_KEXT, DEFAULT_KINT

_LOGGER = logging.getLogger(__name__)


class AdaptiveTPIAlgorithm:
    """Minimal runtime-safe Adaptive TPI algorithm scaffold."""

    def __init__(
        self,
        name: str,
        max_on_percent: float | None = None,
        debug_mode: bool = False,
    ) -> None:
        """Initialize the algorithm scaffold."""
        self._name = name
        self._debug_mode = debug_mode
        self._max_on_percent = max_on_percent
        self._state = AdaptiveTPIState(
            k_int=DEFAULT_KINT,
            k_ext=DEFAULT_KEXT,
        )
        self._supervisor = AdaptiveTPISupervisor(phase=self._state.bootstrap_phase)
        self._temperature_available = False

    def calculate(
        self,
        target_temp: float | None,
        current_temp: float | None,
        ext_current_temp: float | None,
        slope: float | None,
        hvac_mode,
        **kwargs,
    ) -> None:
        """Compute the current on_percent using the placeholder controller."""
        del slope

        self._supervisor.evaluate_runtime_conditions(
            target_temp=target_temp,
            current_temp=current_temp,
            outdoor_temp=ext_current_temp,
            hvac_mode=hvac_mode,
            power_shedding=bool(kwargs.get("power_shedding", False)),
            heating_failure=bool(kwargs.get("heating_failure", False)),
            cycle_interrupted=bool(kwargs.get("cycle_interrupted", False)),
            central_boiler_unavailable=bool(kwargs.get("central_boiler_unavailable", False)),
            startup_blackout_active=bool(kwargs.get("startup_blackout_active", False)),
            setpoint_transition_active=bool(kwargs.get("setpoint_transition_active", False)),
        )
        self._supervisor.apply_to_state(self._state)

        if target_temp is None or current_temp is None:
            self._state.calculated_on_percent = 0.0
            self._temperature_available = False
            return

        self._temperature_available = True
        self._state.calculated_on_percent = compute_on_percent(
            hvac_mode=hvac_mode,
            target_temp=target_temp,
            current_temp=current_temp,
            outdoor_temp=ext_current_temp,
            k_int=self._state.k_int,
            k_ext=self._state.k_ext,
            max_on_percent=self._max_on_percent,
        )
        self._state.on_percent = self._state.calculated_on_percent

    def update_realized_power(self, power_percent: float) -> None:
        """Record the power effectively applied after scheduler constraints."""
        self._state.on_percent = max(0.0, min(1.0, power_percent))

    def save_state(self) -> dict:
        """Return a persistable algorithm snapshot."""
        return self._state.to_persisted_dict()

    def load_state(self, data: Mapping[str, Any] | None) -> None:
        """Load a persistable algorithm snapshot."""
        if not data:
            return
        try:
            self._state.apply_persisted_dict(data)
            self._supervisor.set_phase(self._state.bootstrap_phase)
            self._supervisor.apply_to_state(self._state)
        except (AttributeError, TypeError, ValueError) as err:
            _LOGGER.warning(
                "%s - Ignoring invalid persisted Adaptive TPI state: %s",
                self._name,
                err,
            )

    def get_diagnostics(self) -> dict:
        """Return a compact diagnostics payload."""
        self._supervisor.apply_to_state(self._state)
        return build_diagnostics(self._state, self._debug_mode)

    def reject_cycle(self, reason: str) -> None:
        """Record a hard cycle rejection through the supervisor."""
        self._supervisor.reject_cycle(reason)
        self._supervisor.apply_to_state(self._state)

    def mark_non_informative_cycle(self, reason: str = "non_informative_cycle") -> None:
        """Record a valid but non-informative cycle through the supervisor."""
        self._supervisor.mark_non_informative(reason)
        self._supervisor.apply_to_state(self._state)

    def accept_cycle(self) -> None:
        """Record an accepted cycle through the supervisor."""
        self._supervisor.accept_cycle()
        self._supervisor.apply_to_state(self._state)

    @property
    def on_percent(self) -> float | None:
        """Return the currently applied heating fraction."""
        if not self._temperature_available:
            return None
        return self._state.on_percent

    @property
    def calculated_on_percent(self) -> float:
        """Return the raw calculated heating fraction."""
        return self._state.calculated_on_percent
