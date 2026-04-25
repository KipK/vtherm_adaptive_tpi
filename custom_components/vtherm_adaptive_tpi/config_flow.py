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
    THERMOSTAT_TYPE_VALVE,
)

ERROR_INVALID_VALVE_CURVE = "invalid_valve_curve"


def build_options_main_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Build the Adaptive TPI main defaults schema."""
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
                CONF_VALVE_CURVE_COMPENSATION_ENABLED,
                default=defaults[CONF_VALVE_CURVE_COMPENSATION_ENABLED],
            ): bool,
            vol.Optional(
                CONF_ADAPTIVE_TPI_DEBUG,
                default=defaults[CONF_ADAPTIVE_TPI_DEBUG],
            ): bool,
        }
    )


def build_valve_curve_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Build the Adaptive TPI valve curve schema."""
    return vol.Schema(
        {
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
        }
    )


def build_options_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Build the complete Adaptive TPI defaults schema."""
    schema = {}
    schema.update(build_options_main_schema(defaults).schema)
    schema.update(build_valve_curve_schema(defaults).schema)
    return vol.Schema(schema)


def build_user_target_schema() -> vol.Schema:
    """Build the Adaptive TPI target thermostat schema."""
    return vol.Schema(
        {
            vol.Required(CONF_TARGET_VTHERM): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=CLIMATE_DOMAIN)
            )
        }
    )


def build_user_main_schema(
    defaults: dict[str, Any],
    include_valve_compensation: bool,
) -> vol.Schema:
    """Build the Adaptive TPI per-thermostat main schema."""
    schema = dict(build_options_main_schema(defaults).schema)
    if not include_valve_compensation:
        schema = {
            key: value
            for key, value in schema.items()
            if getattr(key, "schema", key) != CONF_VALVE_CURVE_COMPENSATION_ENABLED
        }
    return vol.Schema(schema)


def build_user_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Build the complete Adaptive TPI per-thermostat schema."""
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


def _with_hidden_defaults(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """Return submitted data completed with hidden fixed defaults."""
    data = dict(user_input)
    data[CONF_ACTUATOR_MODE_OVERRIDE] = ACTUATOR_MODE_AUTO
    return data


def _is_valve_state(state: Any) -> bool:
    """Return whether a VTherm state exposes a valve command space."""
    attributes = getattr(state, "attributes", {}) or {}
    configuration = attributes.get("configuration") or {}
    return (
        configuration.get("type") == THERMOSTAT_TYPE_VALVE
        or configuration.get("have_valve_regulation") is True
    )


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
    _pending_global_data: dict[str, Any] | None = None
    _pending_thermostat_data: dict[str, Any] | None = None
    _pending_thermostat_entity_id: str | None = None
    _pending_thermostat_is_valve: bool = False
    _pending_thermostat_title: str | None = None

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
            self._pending_global_data = _with_hidden_defaults(user_input)
            if user_input.get(CONF_VALVE_CURVE_COMPENSATION_ENABLED):
                return self.async_show_form(
                    step_id="global_valve_curve",
                    data_schema=build_valve_curve_schema(_schema_defaults(user_input)),
                )
            return self.async_create_entry(
                title="Adaptive TPI defaults",
                data=self._pending_global_data,
            )

        return self.async_show_form(
            step_id="global",
            data_schema=build_options_main_schema(DEFAULT_OPTIONS),
        )

    async def async_step_global_valve_curve(self, user_input: dict[str, Any] | None = None):
        """Handle the global valve curve defaults entry."""
        data = dict(self._pending_global_data or _with_hidden_defaults({}))

        if user_input is not None:
            data.update(user_input)
            errors = _validate_valve_curve_config(_schema_defaults(data))
            if errors:
                return self.async_show_form(
                    step_id="global_valve_curve",
                    data_schema=build_valve_curve_schema(_schema_defaults(data)),
                    errors=errors,
                )
            return self.async_create_entry(title="Adaptive TPI defaults", data=data)

        return self.async_show_form(
            step_id="global_valve_curve",
            data_schema=build_valve_curve_schema(_schema_defaults(data)),
        )

    async def async_step_thermostat(self, user_input: dict[str, Any] | None = None):
        """Select the target thermostat."""
        if user_input is not None:
            entity_id = user_input.get(CONF_TARGET_VTHERM)
            registry = er.async_get(self.hass)
            reg_entry = registry.async_get(entity_id)
            if reg_entry is None or reg_entry.unique_id is None:
                return self.async_show_form(
                    step_id="thermostat",
                    data_schema=build_user_target_schema(),
                    errors={CONF_TARGET_VTHERM: "invalid_entity"},
                )

            target_unique_id = reg_entry.unique_id
            await self.async_set_unique_id(f"{DOMAIN}-{target_unique_id}")
            self._abort_if_unique_id_configured()

            state = self.hass.states.get(entity_id)
            self._pending_thermostat_data = {
                CONF_TARGET_VTHERM: target_unique_id,
                CONF_ACTUATOR_MODE_OVERRIDE: ACTUATOR_MODE_AUTO,
            }
            self._pending_thermostat_entity_id = entity_id
            self._pending_thermostat_is_valve = state is not None and _is_valve_state(state)
            self._pending_thermostat_title = state.name if state is not None else entity_id
            return await self.async_step_thermostat_settings()

        return self.async_show_form(
            step_id="thermostat",
            data_schema=build_user_target_schema(),
        )

    async def async_step_thermostat_settings(
        self, user_input: dict[str, Any] | None = None
    ):
        """Handle the per-thermostat main settings entry."""
        data = dict(self._pending_thermostat_data or _with_hidden_defaults({}))

        if user_input is not None:
            data.update(user_input)
            self._pending_thermostat_data = data
            if (
                self._pending_thermostat_is_valve
                and user_input.get(CONF_VALVE_CURVE_COMPENSATION_ENABLED)
            ):
                return self.async_show_form(
                    step_id="thermostat_valve_curve",
                    data_schema=build_valve_curve_schema(_schema_defaults(user_input)),
                )

            return self.async_create_entry(
                title=self._pending_thermostat_title or self._pending_thermostat_entity_id,
                data=data,
            )

        return self.async_show_form(
            step_id="thermostat_settings",
            data_schema=build_user_main_schema(
                _schema_defaults(data),
                self._pending_thermostat_is_valve,
            ),
        )

    async def async_step_thermostat_valve_curve(
        self, user_input: dict[str, Any] | None = None
    ):
        """Handle the per-thermostat valve curve entry."""
        data = dict(self._pending_thermostat_data or _with_hidden_defaults({}))

        if user_input is not None:
            data.update(user_input)
            errors = _validate_valve_curve_config(_schema_defaults(data))
            if errors:
                return self.async_show_form(
                    step_id="thermostat_valve_curve",
                    data_schema=build_valve_curve_schema(_schema_defaults(data)),
                    errors=errors,
                )
            return self.async_create_entry(
                title=self._pending_thermostat_title or self._pending_thermostat_entity_id,
                data=data,
            )

        return self.async_show_form(
            step_id="thermostat_valve_curve",
            data_schema=build_valve_curve_schema(_schema_defaults(data)),
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
        self._pending_options_data: dict[str, Any] | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Handle the options flow."""
        defaults = dict(DEFAULT_OPTIONS)
        defaults.update(self._config_entry.options or self._config_entry.data)

        if user_input is not None:
            submitted_defaults = dict(defaults)
            submitted_defaults.update(_with_hidden_defaults(user_input))
            self._pending_options_data = submitted_defaults
            if user_input.get(CONF_VALVE_CURVE_COMPENSATION_ENABLED):
                return self.async_show_form(
                    step_id="valve_curve",
                    data_schema=build_valve_curve_schema(submitted_defaults),
                )
            return self.async_create_entry(title="", data=submitted_defaults)

        return self.async_show_form(
            step_id="init",
            data_schema=build_options_main_schema(defaults),
        )

    async def async_step_valve_curve(self, user_input: dict[str, Any] | None = None):
        """Handle the options valve curve flow."""
        defaults = dict(DEFAULT_OPTIONS)
        defaults.update(self._config_entry.options or self._config_entry.data)
        data = dict(self._pending_options_data or defaults)

        if user_input is not None:
            data.update(user_input)
            errors = _validate_valve_curve_config(data)
            if errors:
                return self.async_show_form(
                    step_id="valve_curve",
                    data_schema=build_valve_curve_schema(_schema_defaults(data)),
                    errors=errors,
                )
            return self.async_create_entry(title="", data=data)

        return self.async_show_form(
            step_id="valve_curve",
            data_schema=build_valve_curve_schema(_schema_defaults(data)),
        )
