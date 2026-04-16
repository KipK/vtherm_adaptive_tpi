"""Config flow for the vtherm_adaptive_tpi integration."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries

from .const import (
    CONF_ADAPTIVE_TPI_DEBUG,
    CONF_MINIMAL_ACTIVATION_DELAY,
    CONF_MINIMAL_DEACTIVATION_DELAY,
    CONF_TARGET_VTHERM,
    DEFAULT_ADAPTIVE_TPI_DEBUG,
    DEFAULT_MINIMAL_ACTIVATION_DELAY,
    DEFAULT_MINIMAL_DEACTIVATION_DELAY,
    DOMAIN,
)


def _build_schema(defaults: dict | None = None) -> vol.Schema:
    """Build the config form schema."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Optional(
                CONF_TARGET_VTHERM,
                default=defaults.get(CONF_TARGET_VTHERM, ""),
            ): str,
            vol.Optional(
                CONF_MINIMAL_ACTIVATION_DELAY,
                default=defaults.get(
                    CONF_MINIMAL_ACTIVATION_DELAY,
                    DEFAULT_MINIMAL_ACTIVATION_DELAY,
                ),
            ): vol.Coerce(int),
            vol.Optional(
                CONF_MINIMAL_DEACTIVATION_DELAY,
                default=defaults.get(
                    CONF_MINIMAL_DEACTIVATION_DELAY,
                    DEFAULT_MINIMAL_DEACTIVATION_DELAY,
                ),
            ): vol.Coerce(int),
            vol.Optional(
                CONF_ADAPTIVE_TPI_DEBUG,
                default=defaults.get(
                    CONF_ADAPTIVE_TPI_DEBUG,
                    DEFAULT_ADAPTIVE_TPI_DEBUG,
                ),
            ): bool,
        }
    )


class AdaptiveTPIConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for vtherm_adaptive_tpi."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=_build_schema())

        target_vtherm = user_input.get(CONF_TARGET_VTHERM, "").strip()
        unique_id = target_vtherm or DOMAIN
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        title = f"Adaptive TPI - {target_vtherm}" if target_vtherm else "Adaptive TPI"
        return self.async_create_entry(title=title, data=user_input)

