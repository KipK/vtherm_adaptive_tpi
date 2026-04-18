"""Adaptive TPI algorithm handler for the plugin runtime."""

from __future__ import annotations

import logging
from pathlib import Path
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
        self._storage_key = storage_key
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
                power_shedding=t.is_overpowering_detected,
                off_reason=t.hvac_off_reason,
                cycle_min=t.cycle_min,
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

        if t.cycle_scheduler is None:
            _LOGGER.warning(
                "%s - Adaptive TPI scheduler is unavailable. Skipping cycle control.",
                t,
            )
            return

        on_percent = t.on_percent
        if on_percent is None:
            if hasattr(t.prop_algorithm, "reject_cycle"):
                t.prop_algorithm.reject_cycle("missing_temperature")
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
        if scheduler is None:
            _LOGGER.warning(
                "%s - Adaptive TPI received a null scheduler binding",
                self._thermostat,
            )
            return

        scheduler.register_cycle_start_callback(self._on_cycle_started)
        scheduler.register_cycle_end_callback(self._on_cycle_completed)

    async def _on_cycle_started(
        self,
        *,
        on_time_sec: float,
        off_time_sec: float,
        on_percent: float,
        hvac_mode,
    ) -> None:
        """Forward the committed cycle start to the adaptive algorithm."""
        t = self._thermostat
        if t.prop_algorithm is None or not hasattr(t.prop_algorithm, "on_cycle_started"):
            return

        t.prop_algorithm.on_cycle_started(
            on_time_sec=on_time_sec,
            off_time_sec=off_time_sec,
            on_percent=on_percent,
            hvac_mode=hvac_mode,
            target_temp=t.target_temperature,
            current_temp=t.current_temperature,
            ext_current_temp=t.current_outdoor_temperature,
        )

    async def _on_cycle_completed(
        self,
        *,
        e_eff: float | None = None,
        elapsed_ratio: float = 1.0,
        cycle_duration_min: float | None = None,
        **kwargs,
    ) -> None:
        """Forward the completed cycle boundary to the adaptive algorithm."""
        del kwargs
        t = self._thermostat
        if t.prop_algorithm is None or not hasattr(t.prop_algorithm, "on_cycle_completed"):
            return

        t.prop_algorithm.on_cycle_completed(
            e_eff=e_eff,
            elapsed_ratio=elapsed_ratio,
            cycle_duration_min=cycle_duration_min,
            target_temp=t.target_temperature,
            current_temp=t.current_temperature,
            ext_current_temp=t.current_outdoor_temperature,
            hvac_mode=t.vtherm_hvac_mode,
            power_shedding=t.is_overpowering_detected,
        )
        await self._async_save_persisted_state()

    def update_attributes(self) -> None:
        """Expose the current Adaptive TPI diagnostics on the thermostat."""
        t = self._thermostat
        if t.prop_algorithm is None:
            return

        t._attr_extra_state_attributes["specific_states"].update({
            "adaptive_tpi": t.prop_algorithm.get_diagnostics(),
        })

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

        t.prop_algorithm.load_state(
            state_data,
            current_cycle_min=float(t.cycle_min),
            persisted_cycle_min=data.get("cycle_min"),
            last_accepted_at=data.get("last_accepted_at"),
            saved_at=data.get("saved_at"),
        )

    async def _async_save_persisted_state(self) -> None:
        """Save the minimal adaptive state required across reloads."""
        t = self._thermostat
        if t.prop_algorithm is None:
            return

        if (
            hasattr(t.prop_algorithm, "has_pending_learning_update")
            and not t.prop_algorithm.has_pending_learning_update
        ):
            return

        metadata = {}
        if hasattr(t.prop_algorithm, "persistence_metadata"):
            metadata = t.prop_algorithm.persistence_metadata(cycle_min=float(t.cycle_min))

        payload = {
            "schema_version": PERSISTENCE_SCHEMA_VERSION,
            **metadata,
            "state": t.prop_algorithm.save_state(),
        }
        await self._store.async_save(payload)

    async def service_reset_learning(self) -> None:
        """Reset the runtime learning state and purge any persisted snapshot."""
        t = self._thermostat
        if t.prop_algorithm is None or not hasattr(t.prop_algorithm, "reset_learning"):
            return

        t.prop_algorithm.reset_learning()
        await self._async_delete_persisted_state()
        self.update_attributes()
        t.async_write_ha_state()

    async def _async_delete_persisted_state(self) -> None:
        """Delete the persisted snapshot file for this thermostat, if present."""
        storage_path = Path(self._thermostat.hass.config.path(".storage", self._storage_key))
        try:
            storage_path.unlink(missing_ok=True)
        except OSError as err:
            _LOGGER.warning(
                "%s - Unable to delete persisted Adaptive TPI state %s: %s",
                self._thermostat,
                storage_path,
                err,
            )
