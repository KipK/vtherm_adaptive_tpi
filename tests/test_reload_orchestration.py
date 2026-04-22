"""Reload orchestration tests for the Adaptive TPI plugin entry."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.vtherm_adaptive_tpi import (
    CONF_TARGET_VTHERM,
    DATA_SKIP_FULL_RELOAD,
    DATA_SKIP_VT_RELOAD_ON_UNLOAD,
    DOMAIN,
    _async_update_options,
    async_unload_entry,
)


class _DummyConfigEntries:
    """Track config-entry reload requests."""

    def __init__(self) -> None:
        self.reload_calls: list[str] = []

    async def async_reload(self, entry_id: str) -> None:
        """Record one config-entry reload."""
        self.reload_calls.append(entry_id)


class _DummyServices:
    """Minimal Home Assistant service registry stub."""

    def async_remove(self, domain: str, service: str) -> None:
        """Accept service removals during unload."""
        del domain, service


class _DummyHass:
    """Minimal Home Assistant stub for reload orchestration."""

    def __init__(self, running_state) -> None:
        self.data: dict = {}
        self.state = running_state
        self.config_entries = _DummyConfigEntries()
        self.services = _DummyServices()


@pytest.mark.asyncio
async def test_update_options_marks_internal_reload_before_plugin_reload(monkeypatch) -> None:
    """Internal plugin reloads should defer VT reloads until the plugin is ready again."""
    from custom_components.vtherm_adaptive_tpi import CoreState
    import custom_components.vtherm_adaptive_tpi as integration

    hass = _DummyHass(CoreState.running)
    entry = SimpleNamespace(entry_id="plugin-entry", data={CONF_TARGET_VTHERM: "vt-1"})
    vt_reload_targets: list[str] = []

    async def _reload_with_assertions(entry_id: str) -> None:
        assert entry_id == "plugin-entry"
        assert hass.data[DOMAIN][f"{DATA_SKIP_FULL_RELOAD}_{entry_id}"] is True
        assert hass.data[DOMAIN][f"{DATA_SKIP_VT_RELOAD_ON_UNLOAD}_{entry_id}"] is True
        hass.config_entries.reload_calls.append(entry_id)

    async def _reload_target(_hass, target_unique_id: str) -> None:
        del _hass
        vt_reload_targets.append(target_unique_id)

    monkeypatch.setattr(hass.config_entries, "async_reload", _reload_with_assertions)
    monkeypatch.setattr(integration, "_reload_adaptive_tpi_vtherms_for_target", _reload_target)

    await _async_update_options(hass, entry)

    assert hass.config_entries.reload_calls == ["plugin-entry"]
    assert vt_reload_targets == ["vt-1"]


@pytest.mark.asyncio
async def test_unload_skips_vt_reload_during_internal_plugin_reload(monkeypatch) -> None:
    """Internal plugin reloads should not reload VT entries from the unload path."""
    from custom_components.vtherm_adaptive_tpi import CoreState
    import custom_components.vtherm_adaptive_tpi as integration

    hass = _DummyHass(CoreState.running)
    entry = SimpleNamespace(entry_id="plugin-entry")
    hass.data[DOMAIN] = {
        entry.entry_id: entry.entry_id,
        f"{DATA_SKIP_VT_RELOAD_ON_UNLOAD}_{entry.entry_id}": True,
    }
    vt_reload_calls = 0

    async def _reload_vtherms(_hass) -> None:
        nonlocal vt_reload_calls
        del _hass
        vt_reload_calls += 1

    monkeypatch.setattr(integration, "_reload_adaptive_tpi_vtherms", _reload_vtherms)
    monkeypatch.setattr(integration, "_unregister_factory", lambda _hass: None)
    monkeypatch.setattr(integration, "_unregister_services", lambda _hass: None)

    await async_unload_entry(hass, entry)

    assert vt_reload_calls == 0
    assert hass.data[DOMAIN] == {}
