"""The vtherm_adaptive_tpi integration."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import service as service_helper
from vtherm_api.log_collector import get_vtherm_logger
from vtherm_api.vtherm_api import VThermAPI

from .const import (
    CONF_TARGET_VTHERM,
    DATA_FACTORY_REGISTERED,
    DATA_SERVICES_REGISTERED,
    DOMAIN,
    PROP_FUNCTION_ADAPTIVE_TPI,
    SERVICE_RESET_LEARNING,
)
from .factory import AdaptiveTPIHandlerFactory

_LOGGER = get_vtherm_logger(__name__)
VT_DOMAIN = "versatile_thermostat"
DATA_SKIP_FULL_RELOAD = "skip_full_reload"
DATA_SKIP_VT_RELOAD_ON_UNLOAD = "skip_vt_reload_on_unload"


def _ensure_domain_data(hass: HomeAssistant) -> dict[str, Any]:
    """Return the plugin data storage in hass."""
    return hass.data.setdefault(DOMAIN, {})


def _active_entry_keys(data: dict[str, Any]) -> list[str]:
    """Return only actual config-entry keys stored in domain data."""
    return [
        key
        for key in data
        if key not in {DATA_FACTORY_REGISTERED, DATA_SERVICES_REGISTERED}
        and not key.startswith(f"{DATA_SKIP_FULL_RELOAD}_")
        and not key.startswith(f"{DATA_SKIP_VT_RELOAD_ON_UNLOAD}_")
    ]


def _register_factory(hass: HomeAssistant) -> bool:
    """Register the Adaptive TPI factory in the shared VT API."""
    data = _ensure_domain_data(hass)
    if data.get(DATA_FACTORY_REGISTERED) is True:
        return True

    api = VThermAPI.get_vtherm_api(hass)
    if api is None:
        _LOGGER.warning(
            "Unable to register Adaptive TPI factory because VThermAPI is unavailable"
        )
        return False

    factory = AdaptiveTPIHandlerFactory()
    existing_factory = api.get_prop_algorithm(factory.name)
    if existing_factory is None:
        api.register_prop_algorithm(factory)

    data[DATA_FACTORY_REGISTERED] = True
    return True


def _unregister_factory(hass: HomeAssistant) -> None:
    """Unregister the Adaptive TPI factory from the shared VT API."""
    api = VThermAPI.get_vtherm_api(hass)
    if api is not None:
        api.unregister_prop_algorithm(AdaptiveTPIHandlerFactory().name)
    _ensure_domain_data(hass)[DATA_FACTORY_REGISTERED] = False


def _register_services(hass: HomeAssistant) -> None:
    """Register Adaptive TPI services on the plugin domain."""
    data = _ensure_domain_data(hass)
    if data.get(DATA_SERVICES_REGISTERED) is True:
        return

    async def _call_on_vtherms(call, method_name: str) -> None:
        entity_ids = set(await service_helper.async_extract_entity_ids(call))

        call_target = getattr(call, "target", None)
        if isinstance(call_target, dict):
            target_entity_ids = call_target.get("entity_id")
            if isinstance(target_entity_ids, str):
                entity_ids.add(target_entity_ids)
            elif isinstance(target_entity_ids, list):
                entity_ids.update(
                    entity_id for entity_id in target_entity_ids if isinstance(entity_id, str)
                )

        explicit_entity_ids = call.data.get("entity_id")
        if isinstance(explicit_entity_ids, str):
            entity_ids.add(explicit_entity_ids)
        elif isinstance(explicit_entity_ids, list):
            entity_ids.update(
                entity_id for entity_id in explicit_entity_ids if isinstance(entity_id, str)
            )

        component = hass.data.get(CLIMATE_DOMAIN)
        if not component or not entity_ids:
            _LOGGER.warning(
                "Adaptive TPI service %s ignored: no target climate entities resolved",
                method_name,
            )
            return

        invoked_count = 0
        missing_entity_ids: list[str] = []
        for entity_id in sorted(entity_ids):
            entity = component.get_entity(entity_id) if hasattr(component, "get_entity") else None
            if entity is None:
                entity = next(
                    (candidate for candidate in list(component.entities) if candidate.entity_id == entity_id),
                    None,
                )
            if entity is None:
                missing_entity_ids.append(entity_id)
                continue

            handler = getattr(entity, method_name, None)
            algo_handler = getattr(entity, "_algo_handler", None)
            if handler is None and algo_handler is not None:
                handler = getattr(algo_handler, method_name, None)
            if handler is None:
                _LOGGER.warning(
                    "Service %s not available on %s", method_name, entity.entity_id
                )
                continue

            proportional_function = getattr(entity, "proportional_function", None)
            if (
                proportional_function != PROP_FUNCTION_ADAPTIVE_TPI
                and getattr(algo_handler, "__class__", type(None)).__module__.split(".")[-1] != "handler"
            ):
                _LOGGER.warning(
                    "Service %s ignored on %s because it is not using Adaptive TPI",
                    method_name,
                    entity.entity_id,
                )
                continue

            await handler()
            invoked_count += 1

        if missing_entity_ids:
            _LOGGER.warning(
                "Adaptive TPI service %s could not resolve climate entities: %s",
                method_name,
                missing_entity_ids,
            )

        if invoked_count == 0:
            _LOGGER.warning(
                "Adaptive TPI service %s did not match any active thermostat handler for %s",
                method_name,
                sorted(entity_ids),
            )
        else:
            _LOGGER.info(
                "Adaptive TPI service %s applied to %d thermostat(s): %s",
                method_name,
                invoked_count,
                sorted(entity_ids),
            )

    async def _handle_reset_learning(call) -> None:
        """Handle the Adaptive TPI reset service with a real async callable."""
        await _call_on_vtherms(call, "service_reset_learning")

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_LEARNING,
        _handle_reset_learning,
    )
    data[DATA_SERVICES_REGISTERED] = True


def _unregister_services(hass: HomeAssistant) -> None:
    """Unregister Adaptive TPI services from the plugin domain."""
    hass.services.async_remove(DOMAIN, SERVICE_RESET_LEARNING)
    _ensure_domain_data(hass)[DATA_SERVICES_REGISTERED] = False


async def _reload_adaptive_tpi_vtherms(hass: HomeAssistant) -> None:
    """Reload VT entries that currently target the Adaptive TPI algorithm."""
    reload_tasks = [
        hass.config_entries.async_reload(entry.entry_id)
        for entry in hass.config_entries.async_entries(VT_DOMAIN)
        if entry.data.get("proportional_function") == PROP_FUNCTION_ADAPTIVE_TPI
    ]
    if reload_tasks:
        await asyncio.gather(*reload_tasks)


def _get_dedicated_target_unique_ids(hass: HomeAssistant) -> set[str]:
    """Return VT unique ids that have a dedicated Adaptive TPI config entry."""
    return {
        plugin_entry.data[CONF_TARGET_VTHERM]
        for plugin_entry in hass.config_entries.async_entries(DOMAIN)
        if plugin_entry.data.get(CONF_TARGET_VTHERM)
    }


async def _reload_adaptive_tpi_vtherms_for_target(
    hass: HomeAssistant,
    target_unique_id: str,
) -> None:
    """Reload the VT entry bound to one given thermostat unique id."""
    registry = er.async_get(hass)
    climate_entity_id = registry.async_get_entity_id(
        CLIMATE_DOMAIN,
        "versatile_thermostat",
        target_unique_id,
    )
    if not climate_entity_id:
        return

    climate_entry = registry.async_get(climate_entity_id)
    if climate_entry is None or climate_entry.config_entry_id is None:
        return

    await hass.config_entries.async_reload(climate_entry.config_entry_id)


async def _reload_adaptive_tpi_vtherms_using_defaults(hass: HomeAssistant) -> None:
    """Reload Adaptive TPI VT entries that do not have a dedicated plugin entry."""
    dedicated_target_unique_ids = _get_dedicated_target_unique_ids(hass)
    component = hass.data.get(CLIMATE_DOMAIN)
    if not component:
        return

    registry = er.async_get(hass)
    vt_entry_ids: set[str] = set()
    for entity in component.entities:
        if getattr(entity, "proportional_function", None) != PROP_FUNCTION_ADAPTIVE_TPI:
            continue
        if getattr(entity, "unique_id", None) in dedicated_target_unique_ids:
            continue

        climate_entry = registry.async_get(entity.entity_id)
        if climate_entry is None or climate_entry.config_entry_id is None:
            continue
        vt_entry_ids.add(climate_entry.config_entry_id)

    if vt_entry_ids:
        await asyncio.gather(
            *(hass.config_entries.async_reload(entry_id) for entry_id in vt_entry_ids)
        )


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up vtherm_adaptive_tpi from YAML."""
    del config
    _register_factory(hass)
    _register_services(hass)
    return True


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload affected VT entries when Adaptive TPI options change."""
    data = _ensure_domain_data(hass)
    data[f"{DATA_SKIP_FULL_RELOAD}_{entry.entry_id}"] = True
    data[f"{DATA_SKIP_VT_RELOAD_ON_UNLOAD}_{entry.entry_id}"] = True
    await hass.config_entries.async_reload(entry.entry_id)

    target_unique_id = entry.data.get(CONF_TARGET_VTHERM)
    if target_unique_id:
        await _reload_adaptive_tpi_vtherms_for_target(hass, target_unique_id)
        return

    await _reload_adaptive_tpi_vtherms_using_defaults(hass)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up vtherm_adaptive_tpi from a config entry."""
    data = _ensure_domain_data(hass)
    data[entry.entry_id] = entry.entry_id
    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    _register_factory(hass)
    _register_services(hass)
    skip_full_reload = data.pop(f"{DATA_SKIP_FULL_RELOAD}_{entry.entry_id}", False)
    data.pop(f"{DATA_SKIP_VT_RELOAD_ON_UNLOAD}_{entry.entry_id}", False)
    if hass.state == CoreState.running and not skip_full_reload:
        await _reload_adaptive_tpi_vtherms(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a vtherm_adaptive_tpi config entry."""
    data = _ensure_domain_data(hass)
    data.pop(entry.entry_id, None)
    skip_vt_reload_on_unload = data.pop(
        f"{DATA_SKIP_VT_RELOAD_ON_UNLOAD}_{entry.entry_id}",
        False,
    )

    if not _active_entry_keys(data):
        _unregister_factory(hass)
        _unregister_services(hass)

    # Only force VT reloads for live uninstall/reconfiguration.
    # During HA shutdown, reloading VT entries here can recreate entities before
    # RestoreEntity/recorder have dumped the final requested state, which risks
    # persisting an OFF bootstrap state and restoring OFF at next startup.
    if hass.state == CoreState.running and not skip_vt_reload_on_unload:
        await _reload_adaptive_tpi_vtherms(hass)
    return True
