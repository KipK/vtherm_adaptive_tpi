"""The vtherm_adaptive_tpi integration."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import entity_registry as er
from vtherm_api.log_collector import get_vtherm_logger
from vtherm_api.vtherm_api import VThermAPI

from .const import (
    CONF_TARGET_VTHERM,
    DATA_FACTORY_REGISTERED,
    DOMAIN,
    PROP_FUNCTION_ADAPTIVE_TPI,
)
from .factory import AdaptiveTPIHandlerFactory

_LOGGER = get_vtherm_logger(__name__)
VT_DOMAIN = "versatile_thermostat"
DATA_SKIP_FULL_RELOAD = "skip_full_reload"


def _ensure_domain_data(hass: HomeAssistant) -> dict[str, Any]:
    """Return the plugin data storage in hass."""
    return hass.data.setdefault(DOMAIN, {})


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
    return True


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload affected VT entries when Adaptive TPI options change."""
    _ensure_domain_data(hass)[f"{DATA_SKIP_FULL_RELOAD}_{entry.entry_id}"] = True
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
    skip_full_reload = data.pop(f"{DATA_SKIP_FULL_RELOAD}_{entry.entry_id}", False)
    if hass.state == CoreState.running and not skip_full_reload:
        await _reload_adaptive_tpi_vtherms(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a vtherm_adaptive_tpi config entry."""
    data = _ensure_domain_data(hass)
    data.pop(entry.entry_id, None)

    if not [key for key in data if key != DATA_FACTORY_REGISTERED]:
        _unregister_factory(hass)

    await _reload_adaptive_tpi_vtherms(hass)
    return True
