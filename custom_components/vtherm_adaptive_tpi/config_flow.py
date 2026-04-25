"""Config flow for vtherm_adaptive_tpi."""

from __future__ import annotations

from typing import Any, Mapping

import voluptuous as vol
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .const import (
    ACTUATOR_MODE_AUTO,
    CONF_ADAPTIVE_TPI_DEBUG,
    CONF_ACTUATOR_MODE_OVERRIDE,
    CONF_DEFAULT_KEXT,
    CONF_DEFAULT_KINT,
    CONF_MINIMAL_ACTIVATION_DELAY,
    CONF_MINIMAL_DEACTIVATION_DELAY,
    CONF_RESPONSIVENESS,
    CONF_TARGET_VTHERM,
    CONF_VALVE_CURVE_COMPENSATION_ENABLED,
    CONF_VALVE_CURVE_LEARNING_ENABLED,
    CONF_VALVE_KNEE_DEMAND,
    CONF_VALVE_KNEE_VALVE,
    CONF_VALVE_MAX_VALVE,
    CONF_VALVE_MIN_VALVE,
    DEFAULT_OPTIONS,
    DOMAIN,
)

ERROR_INVALID_VALVE_CURVE = "invalid_valve_curve"


def build_options_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Build the Adaptive TPI defaults schema."""
    return vol.Schema(
        {
            vol.Optional(
                CONF_MINIMAL_ACTIVATION_DELAY,
                default=defaults[CONF_MINIMAL_ACTIVATION_DELAY],
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=3600,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                )
            ),
            vol.Optional(
                CONF_MINIMAL_DEACTIVATION_DELAY,
                default=defaults[CONF_MINIMAL_DEACTIVATION_DELAY],
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=3600,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                )
            ),
            vol.Optional(
                CONF_RESPONSIVENESS,
                default=defaults[CONF_RESPONSIVENESS],
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=5,
                    step=1,
                    mode=selector.NumberSelectorMode.SLIDER,
                )
            ),
            vol.Optional(
                CONF_DEFAULT_KINT,
                default=defaults[CONF_DEFAULT_KINT],
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.05,
                    max=1.2,
                    step=0.05,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_DEFAULT_KEXT,
                default=defaults[CONF_DEFAULT_KEXT],
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.0,
                    max=0.3,
                    step=0.005,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_ACTUATOR_MODE_OVERRIDE,
                default=defaults[CONF_ACTUATOR_MODE_OVERRIDE],
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=ACTUATOR_MODE_AUTO, label="Auto"),
                        selector.SelectOptionDict(value="switch", label="Switch"),
                        selector.SelectOptionDict(value="valve", label="Valve"),
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_VALVE_CURVE_COMPENSATION_ENABLED,
                default=defaults[CONF_VALVE_CURVE_COMPENSATION_ENABLED],
            ): bool,
            vol.Optional(
                CONF_VALVE_CURVE_LEARNING_ENABLED,
                default=defaults[CONF_VALVE_CURVE_LEARNING_ENABLED],
            ): bool,
            vol.Optional(
                CONF_VALVE_MIN_VALVE,
                default=defaults[CONF_VALVE_MIN_VALVE],
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=20,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_VALVE_KNEE_DEMAND,
                default=defaults[CONF_VALVE_KNEE_DEMAND],
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=50,
                    max=95,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_VALVE_KNEE_VALVE,
                default=defaults[CONF_VALVE_KNEE_VALVE],
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=10,
                    max=50,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_VALVE_MAX_VALVE,
                default=defaults[CONF_VALVE_MAX_VALVE],
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=50,
                    max=100,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_ADAPTIVE_TPI_DEBUG,
                default=defaults[CONF_ADAPTIVE_TPI_DEBUG],
            ): bool,
        }
    )


def build_user_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Build the Adaptive TPI per-thermostat schema."""
    schema = {
        vol.Required(CONF_TARGET_VTHERM): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=CLIMATE_DOMAIN)
        )
    }
    schema.update(build_options_schema(defaults).schema)
    return vol.Schema(schema)


def _schema_defaults(user_input: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return form defaults merged with the latest submitted values."""
    defaults = dict(DEFAULT_OPTIONS)
    if user_input is not None:
        defaults.update(user_input)
    return defaults


def _validate_valve_curve_config(config: Mapping[str, Any]) -> dict[str, str]:
    """Validate cross-field valve curve constraints."""
    try:
        min_valve = float(config[CONF_VALVE_MIN_VALVE])
        knee_demand = float(config[CONF_VALVE_KNEE_DEMAND])
        knee_valve = float(config[CONF_VALVE_KNEE_VALVE])
        max_valve = float(config[CONF_VALVE_MAX_VALVE])
    except (KeyError, TypeError, ValueError):
        return {"base": ERROR_INVALID_VALVE_CURVE}

    if 0.0 <= min_valve < knee_valve < max_valve <= 100.0 and 0.0 < knee_demand < 100.0:
        return {}
    return {"base": ERROR_INVALID_VALVE_CURVE}


class AdaptiveTPIConfigFlow(ConfigFlow, domain=DOMAIN):
    """Manage Adaptive TPI plugin config entries."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Create default plugin settings on first install."""
        if not self._async_current_entries():
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="Adaptive TPI defaults",
                data=dict(DEFAULT_OPTIONS),
            )

        return await self.async_step_thermostat(user_input)

    async def async_step_global(self, user_input: dict[str, Any] | None = None):
        """Handle the global defaults entry."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            errors = _validate_valve_curve_config(_schema_defaults(user_input))
            if errors:
                return self.async_show_form(
                    step_id="global",
                    data_schema=build_options_schema(_schema_defaults(user_input)),
                    errors=errors,
                )
            return self.async_create_entry(title="Adaptive TPI defaults", data=user_input)

        return self.async_show_form(
            step_id="global",
            data_schema=build_options_schema(DEFAULT_OPTIONS),
        )

    async def async_step_thermostat(self, user_input: dict[str, Any] | None = None):
        """Handle the per-thermostat entry."""
        if user_input is not None:
            entity_id = user_input.get(CONF_TARGET_VTHERM)
            registry = er.async_get(self.hass)
            reg_entry = registry.async_get(entity_id)
            if reg_entry is None or reg_entry.unique_id is None:
                return self.async_show_form(
                    step_id="thermostat",
                    data_schema=build_user_schema(DEFAULT_OPTIONS),
                    errors={CONF_TARGET_VTHERM: "invalid_entity"},
                )

            errors = _validate_valve_curve_config(_schema_defaults(user_input))
            if errors:
                return self.async_show_form(
                    step_id="thermostat",
                    data_schema=build_user_schema(_schema_defaults(user_input)),
                    errors=errors,
                )

            target_unique_id = reg_entry.unique_id
            await self.async_set_unique_id(f"{DOMAIN}-{target_unique_id}")
            self._abort_if_unique_id_configured()

            data = dict(user_input)
            data[CONF_TARGET_VTHERM] = target_unique_id
            state = self.hass.states.get(entity_id)
            title = state.name if state is not None else entity_id
            return self.async_create_entry(title=title, data=data)

        return self.async_show_form(
            step_id="thermostat",
            data_schema=build_user_schema(DEFAULT_OPTIONS),
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        """Return the options flow handler."""
        return AdaptiveTPIOptionsFlow(config_entry)


class AdaptiveTPIOptionsFlow(OptionsFlow):
    """Edit Adaptive TPI plugin defaults."""

    def __init__(self, config_entry) -> None:
        """Store the config entry being edited."""
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Handle the options flow."""
        defaults = dict(DEFAULT_OPTIONS)
        defaults.update(self._config_entry.options or self._config_entry.data)

        if user_input is not None:
            submitted_defaults = dict(defaults)
            submitted_defaults.update(user_input)
            errors = _validate_valve_curve_config(submitted_defaults)
            if errors:
                return self.async_show_form(
                    step_id="init",
                    data_schema=build_options_schema(submitted_defaults),
                    errors=errors,
                )
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=build_options_schema(defaults),
        )
