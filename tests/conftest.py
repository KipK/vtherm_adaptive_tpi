"""Pytest bootstrap for local unit tests."""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_homeassistant_stubs() -> None:
    """Install the minimal Home Assistant modules needed by imports."""
    if "homeassistant" in sys.modules:
        return

    homeassistant = types.ModuleType("homeassistant")
    config_entries = types.ModuleType("homeassistant.config_entries")
    core = types.ModuleType("homeassistant.core")
    components = types.ModuleType("homeassistant.components")
    climate = types.ModuleType("homeassistant.components.climate")
    helpers = types.ModuleType("homeassistant.helpers")
    storage = types.ModuleType("homeassistant.helpers.storage")
    device_registry_mod = types.ModuleType("homeassistant.helpers.device_registry")
    entity_registry_mod = types.ModuleType("homeassistant.helpers.entity_registry")

    class ConfigEntry:  # pragma: no cover - import shim
        """Placeholder config entry type for tests."""

    class HomeAssistant:  # pragma: no cover - import shim
        """Placeholder Home Assistant type for tests."""

    class Store:  # pragma: no cover - import shim
        """Minimal storage shim used by handler imports."""

        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        async def async_load(self):
            """Return no persisted payload in import-only contexts."""
            return None

        async def async_save(self, data) -> None:
            """Accept writes in import-only contexts."""
            del data

    def _stub_dr_async_get(hass):  # pragma: no cover - import shim
        del hass
        return None

    def _stub_er_async_get(hass):  # pragma: no cover - import shim
        del hass
        return None

    config_entries.ConfigEntry = ConfigEntry
    core.HomeAssistant = HomeAssistant
    storage.Store = Store
    climate.DOMAIN = "climate"
    device_registry_mod.async_get = _stub_dr_async_get
    entity_registry_mod.async_get = _stub_er_async_get

    sys.modules["homeassistant"] = homeassistant
    sys.modules["homeassistant.config_entries"] = config_entries
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.components"] = components
    sys.modules["homeassistant.components.climate"] = climate
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.storage"] = storage
    sys.modules["homeassistant.helpers.device_registry"] = device_registry_mod
    sys.modules["homeassistant.helpers.entity_registry"] = entity_registry_mod


def _install_vtherm_api_stubs() -> None:
    """Install the minimal VTherm API modules needed by imports."""
    if "vtherm_api" in sys.modules:
        return

    vtherm_api = types.ModuleType("vtherm_api")
    interfaces = types.ModuleType("vtherm_api.interfaces")
    log_collector = types.ModuleType("vtherm_api.log_collector")
    api_module = types.ModuleType("vtherm_api.vtherm_api")

    class InterfacePropAlgorithmFactory:  # pragma: no cover - import shim
        """Placeholder factory interface for tests."""

    class InterfacePropAlgorithmHandler:  # pragma: no cover - import shim
        """Placeholder handler interface for tests."""

    class InterfaceThermostatRuntime:  # pragma: no cover - import shim
        """Placeholder thermostat runtime interface for tests."""

    class VThermAPI:  # pragma: no cover - import shim
        """Minimal VT API shim used by integration imports."""

        @staticmethod
        def get_vtherm_api(hass):
            """Return no shared API in unit tests."""
            del hass
            return None

    interfaces.InterfacePropAlgorithmFactory = InterfacePropAlgorithmFactory
    interfaces.InterfacePropAlgorithmHandler = InterfacePropAlgorithmHandler
    interfaces.InterfaceThermostatRuntime = InterfaceThermostatRuntime
    log_collector.get_vtherm_logger = logging.getLogger
    api_module.VThermAPI = VThermAPI

    sys.modules["vtherm_api"] = vtherm_api
    sys.modules["vtherm_api.interfaces"] = interfaces
    sys.modules["vtherm_api.log_collector"] = log_collector
    sys.modules["vtherm_api.vtherm_api"] = api_module


_install_homeassistant_stubs()
_install_vtherm_api_stubs()
