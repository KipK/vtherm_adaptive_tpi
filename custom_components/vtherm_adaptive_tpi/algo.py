"""Runtime algorithm wrapper for Adaptive TPI."""

from __future__ import annotations

import logging
from typing import Any, Mapping

from .adaptive_tpi.controller import compute_on_percent
from .adaptive_tpi.diagnostics import build_diagnostics
from .adaptive_tpi.state import AdaptiveTPIState
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
        del kwargs

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
        except (AttributeError, TypeError, ValueError) as err:
            _LOGGER.warning(
                "%s - Ignoring invalid persisted Adaptive TPI state: %s",
                self._name,
                err,
            )

    def get_diagnostics(self) -> dict:
        """Return a compact diagnostics payload."""
        return build_diagnostics(self._state, self._debug_mode)

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
