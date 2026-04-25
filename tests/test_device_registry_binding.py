"""Unit tests for Device Registry binding in AdaptiveTPIHandler."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.vtherm_adaptive_tpi.handler import AdaptiveTPIHandler


def _make_handler(
    *,
    entity_id: str | None = "climate.my_vtherm",
    unique_id: str = "my_vtherm_uid",
    applied_config_entry_id: str | None = "atpi_entry_id",
) -> AdaptiveTPIHandler:
    """Build a bare AdaptiveTPIHandler without touching the Store."""
    thermostat = SimpleNamespace(
        entity_id=entity_id,
        unique_id=unique_id,
        hass=MagicMock(),
    )
    handler = object.__new__(AdaptiveTPIHandler)
    handler._thermostat = thermostat
    handler._applied_config_entry_id = applied_config_entry_id
    return handler


def _make_er_entry(device_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(device_id=device_id)


# ---------------------------------------------------------------------------
# _get_target_device_id
# ---------------------------------------------------------------------------


def test_get_target_device_id_via_entity_id_attribute() -> None:
    """Uses entity_id attr when present and maps it to device_id."""
    handler = _make_handler(entity_id="climate.my_vtherm")
    er_entry = _make_er_entry("device-abc")
    mock_registry = MagicMock()
    mock_registry.async_get.return_value = er_entry

    with patch(
        "custom_components.vtherm_adaptive_tpi.handler.er.async_get",
        return_value=mock_registry,
    ):
        device_id = handler._get_target_device_id()

    assert device_id == "device-abc"
    mock_registry.async_get.assert_called_once_with("climate.my_vtherm")


def test_get_target_device_id_fallback_to_entity_registry_lookup() -> None:
    """Falls back to async_get_entity_id when entity_id attr is absent."""
    handler = _make_handler(entity_id=None, unique_id="uid-xyz")
    er_entry = _make_er_entry("device-fallback")
    mock_registry = MagicMock()
    mock_registry.async_get_entity_id.return_value = "climate.fallback"
    mock_registry.async_get.return_value = er_entry

    with patch(
        "custom_components.vtherm_adaptive_tpi.handler.er.async_get",
        return_value=mock_registry,
    ):
        device_id = handler._get_target_device_id()

    assert device_id == "device-fallback"
    mock_registry.async_get_entity_id.assert_called_once_with(
        "climate", "versatile_thermostat", "uid-xyz"
    )
    mock_registry.async_get.assert_called_once_with("climate.fallback")


def test_get_target_device_id_returns_none_when_entity_not_found() -> None:
    """Returns None when the entity is absent from the Entity Registry."""
    handler = _make_handler(entity_id=None)
    mock_registry = MagicMock()
    mock_registry.async_get_entity_id.return_value = None

    with patch(
        "custom_components.vtherm_adaptive_tpi.handler.er.async_get",
        return_value=mock_registry,
    ):
        device_id = handler._get_target_device_id()

    assert device_id is None


def test_get_target_device_id_returns_none_when_no_device_id_on_entry() -> None:
    """Returns None when the entity entry has no device_id."""
    handler = _make_handler(entity_id="climate.my_vtherm")
    mock_registry = MagicMock()
    mock_registry.async_get.return_value = _make_er_entry(None)

    with patch(
        "custom_components.vtherm_adaptive_tpi.handler.er.async_get",
        return_value=mock_registry,
    ):
        device_id = handler._get_target_device_id()

    assert device_id is None


# ---------------------------------------------------------------------------
# _bind_config_entry_to_device
# ---------------------------------------------------------------------------


def test_bind_calls_async_update_device_with_add() -> None:
    """Binding links the config entry to the device via async_update_device."""
    handler = _make_handler(applied_config_entry_id="atpi-entry-1")
    mock_dr = MagicMock()

    with (
        patch.object(handler, "_get_target_device_id", return_value="dev-111"),
        patch(
            "custom_components.vtherm_adaptive_tpi.handler.dr.async_get",
            return_value=mock_dr,
        ),
    ):
        handler._bind_config_entry_to_device()

    mock_dr.async_update_device.assert_called_once_with(
        "dev-111",
        add_config_entry_id="atpi-entry-1",
    )


def test_bind_no_op_when_no_applied_entry() -> None:
    """No Device Registry call when _applied_config_entry_id is None."""
    handler = _make_handler(applied_config_entry_id=None)

    with patch(
        "custom_components.vtherm_adaptive_tpi.handler.dr.async_get"
    ) as mock_dr_get:
        handler._bind_config_entry_to_device()

    mock_dr_get.assert_not_called()


def test_bind_no_op_when_device_not_found() -> None:
    """No Device Registry call when _get_target_device_id returns None."""
    handler = _make_handler(applied_config_entry_id="atpi-entry-1")

    with (
        patch.object(handler, "_get_target_device_id", return_value=None),
        patch(
            "custom_components.vtherm_adaptive_tpi.handler.dr.async_get"
        ) as mock_dr_get,
    ):
        handler._bind_config_entry_to_device()

    mock_dr_get.assert_not_called()


# ---------------------------------------------------------------------------
# _unbind_config_entry_from_device
# ---------------------------------------------------------------------------


def test_unbind_calls_async_update_device_with_remove() -> None:
    """Unbinding removes the config entry from the device via async_update_device."""
    handler = _make_handler(applied_config_entry_id="atpi-entry-1")
    mock_dr = MagicMock()

    with (
        patch.object(handler, "_get_target_device_id", return_value="dev-222"),
        patch(
            "custom_components.vtherm_adaptive_tpi.handler.dr.async_get",
            return_value=mock_dr,
        ),
    ):
        handler._unbind_config_entry_from_device()

    mock_dr.async_update_device.assert_called_once_with(
        "dev-222",
        remove_config_entry_id="atpi-entry-1",
    )


def test_unbind_no_op_when_no_applied_entry() -> None:
    """No Device Registry call when _applied_config_entry_id is None."""
    handler = _make_handler(applied_config_entry_id=None)

    with patch(
        "custom_components.vtherm_adaptive_tpi.handler.dr.async_get"
    ) as mock_dr_get:
        handler._unbind_config_entry_from_device()

    mock_dr_get.assert_not_called()


def test_unbind_no_op_when_device_not_found() -> None:
    """No Device Registry call when _get_target_device_id returns None."""
    handler = _make_handler(applied_config_entry_id="atpi-entry-1")

    with (
        patch.object(handler, "_get_target_device_id", return_value=None),
        patch(
            "custom_components.vtherm_adaptive_tpi.handler.dr.async_get"
        ) as mock_dr_get,
    ):
        handler._unbind_config_entry_from_device()

    mock_dr_get.assert_not_called()
