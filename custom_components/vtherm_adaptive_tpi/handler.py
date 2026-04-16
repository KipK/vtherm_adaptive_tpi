"""Adaptive TPI algorithm handler for the plugin runtime."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .algo import AdaptiveTPIAlgorithm
from .const import (
    CONF_ADAPTIVE_TPI_DEBUG,
    CONF_MINIMAL_ACTIVATION_DELAY,
    CONF_MINIMAL_DEACTIVATION_DELAY,
    CONF_TARGET_VTHERM,
    DEFAULT_OPTIONS,
    DOMAIN,
)

if TYPE_CHECKING:
    from vtherm_api.interfaces import InterfaceThermostatRuntime

_LOGGER = logging.getLogger(__name__)


class AdaptiveTPIHandler:
    """Handler for Adaptive TPI-specific runtime logic."""

    def __init__(self, thermostat: "InterfaceThermostatRuntime"):
        """Initialize handler with parent thermostat reference."""
        self._thermostat = thermostat
        self._should_publish_intermediate = True

    def init_algorithm(self) -> None:
        """Initialize Adaptive TPI algorithm."""
        t = self._thermostat
        entry = self._get_effective_config()

        t.minimal_activation_delay = entry.get(CONF_MINIMAL_ACTIVATION_DELAY, 0)
        t.minimal_deactivation_delay = entry.get(CONF_MINIMAL_DEACTIVATION_DELAY, 0)

        t.prop_algorithm = AdaptiveTPIAlgorithm(
            name=t.name,
            max_on_percent=getattr(t, "max_on_percent", None),
            debug_mode=bool(entry.get(CONF_ADAPTIVE_TPI_DEBUG, False)),
        )

        _LOGGER.info("%s - Adaptive TPI scaffold initialized", t)

    def _get_effective_config(self) -> dict:
        """Return the merged Adaptive TPI configuration for the thermostat."""
        t = self._thermostat
        config = dict(DEFAULT_OPTIONS)
        config.update(t.entry_infos or {})

        plugin_entries = t.hass.config_entries.async_entries(DOMAIN)
        matching_entry = next(
            (
                entry
                for entry in plugin_entries
                if entry.data.get(CONF_TARGET_VTHERM) == t.unique_id
            ),
            None,
        )
        global_entry = next(
            (entry for entry in plugin_entries if entry.unique_id == DOMAIN),
            None,
        )
        entry_to_apply = matching_entry or global_entry
        if entry_to_apply is not None:
            config.update(entry_to_apply.data)
            config.update(entry_to_apply.options)

        return config

    async def async_added_to_hass(self) -> None:
        """Run startup actions when the thermostat entity is added."""

    async def async_startup(self) -> None:
        """Run startup actions after thermostat initialization."""

    def remove(self) -> None:
        """Release resources held by the handler."""

    async def control_heating(
        self,
        timestamp=None,
        force: bool = False,
    ) -> None:
        """Execute one proportional control iteration."""
        del timestamp
        t = self._thermostat
        self._should_publish_intermediate = force

        if t.prop_algorithm:
            t.prop_algorithm.calculate(
                t.target_temperature,
                t.current_temperature,
                t.current_outdoor_temperature,
                t.last_temperature_slope,
                t.vtherm_hvac_mode,
                power_shedding=t.power_manager.is_overpowering_detected,
                off_reason=t.hvac_off_reason,
            )

        if t.vtherm_hvac_mode is not None and str(t.vtherm_hvac_mode).lower().endswith("off"):
            t._on_time_sec = 0
            t._off_time_sec = int(t.cycle_min * 60)
            if t.is_device_active:
                await t.async_underlying_entity_turn_off()
            elif t.cycle_scheduler and t.cycle_scheduler.is_cycle_running:
                await t.cycle_scheduler.cancel_cycle()
            return

        if t.prop_algorithm is None:
            return

        on_percent = t.on_percent
        if on_percent is None:
            _LOGGER.info(
                "%s - on_percent is None (temperature unavailable). Skipping cycle.",
                t,
            )
            return

        await t.cycle_scheduler.start_cycle(
            t.vtherm_hvac_mode,
            on_percent,
            force,
        )

    async def on_state_changed(self) -> None:
        """React to a thermostat state change."""

    def on_scheduler_ready(self, scheduler) -> None:
        """Bind the handler to the cycle scheduler."""
        del scheduler

    def should_publish_intermediate(self) -> bool:
        """Return True when VT may publish intermediate thermostat states."""
        return self._should_publish_intermediate

