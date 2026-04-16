"""Adaptive TPI algorithm handler for the plugin runtime."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store

from .algo import AdaptiveTPIAlgorithm
from .adaptive_tpi.state import PERSISTENCE_SCHEMA_VERSION
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
_STORAGE_KEY_PREFIX = f"{DOMAIN}.state"


class AdaptiveTPIHandler:
    """Handler for Adaptive TPI-specific runtime logic."""

    def __init__(self, thermostat: "InterfaceThermostatRuntime"):
        """Initialize handler with parent thermostat reference."""
        self._thermostat = thermostat
        self._should_publish_intermediate = True
        storage_key = self._build_storage_key(thermostat.unique_id)
        self._store: Store[dict[str, Any]] = Store(
            thermostat.hass,
            PERSISTENCE_SCHEMA_VERSION,
            storage_key,
        )

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
        await self._async_load_persisted_state()

    async def async_startup(self) -> None:
        """Run startup actions after thermostat initialization."""

    def remove(self) -> None:
        """Release resources held by the handler."""
        self._thermostat.hass.async_create_task(self._async_save_persisted_state())

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

    @staticmethod
    def _build_storage_key(unique_id: str) -> str:
        """Build a stable storage key for one thermostat instance."""
        return f"{_STORAGE_KEY_PREFIX}.{unique_id.replace('.', '_')}"

    async def _async_load_persisted_state(self) -> None:
        """Load the persisted state and keep defaults on any invalid payload."""
        t = self._thermostat
        if t.prop_algorithm is None:
            return

        data = await self._store.async_load()
        if not data:
            return

        if not isinstance(data, dict):
            _LOGGER.warning(
                "%s - Ignoring persisted Adaptive TPI state because payload is not a mapping",
                t,
            )
            return

        schema_version = data.get("schema_version")
        if schema_version != PERSISTENCE_SCHEMA_VERSION:
            _LOGGER.info(
                "%s - Ignoring persisted Adaptive TPI state due to unsupported schema version: %s",
                t,
                schema_version,
            )
            return

        state_data = data.get("state")
        if not isinstance(state_data, dict):
            _LOGGER.warning(
                "%s - Ignoring persisted Adaptive TPI state because 'state' is invalid",
                t,
            )
            return

        t.prop_algorithm.load_state(state_data)

    async def _async_save_persisted_state(self) -> None:
        """Save the minimal adaptive state required across reloads."""
        t = self._thermostat
        if t.prop_algorithm is None:
            return

        payload = {
            "schema_version": PERSISTENCE_SCHEMA_VERSION,
            "state": t.prop_algorithm.save_state(),
        }
        await self._store.async_save(payload)
