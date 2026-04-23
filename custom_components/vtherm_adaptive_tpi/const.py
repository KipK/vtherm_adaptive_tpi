"""Constants for the vtherm_adaptive_tpi integration."""

from __future__ import annotations

DOMAIN = "vtherm_adaptive_tpi"
NAME = "Versatile Thermostat Adaptive TPI"

CONF_TARGET_VTHERM = "target_vtherm_unique_id"
CONF_MINIMAL_ACTIVATION_DELAY = "minimal_activation_delay"
CONF_MINIMAL_DEACTIVATION_DELAY = "minimal_deactivation_delay"
CONF_ADAPTIVE_TPI_DEBUG = "adaptive_tpi_debug"
CONF_RESPONSIVENESS = "responsiveness"
CONF_DEFAULT_KINT = "default_kint"
CONF_DEFAULT_KEXT = "default_kext"

ACTUATOR_MODE_SWITCH = "switch"
ACTUATOR_MODE_VALVE = "valve"
CONF_THERMOSTAT_TYPE_KEY = "thermostat_type"
CONF_AUTO_REGULATION_MODE_KEY = "auto_regulation_mode"
THERMOSTAT_TYPE_VALVE = "thermostat_over_valve"
THERMOSTAT_TYPE_CLIMATE = "thermostat_over_climate"
# Mirror of versatile_thermostat.const.CONF_AUTO_REGULATION_VALVE.
AUTO_REGULATION_VALVE = "auto_regulation_valve"

DEFAULT_MINIMAL_ACTIVATION_DELAY = 0
DEFAULT_MINIMAL_DEACTIVATION_DELAY = 0
DEFAULT_ADAPTIVE_TPI_DEBUG = False
DEFAULT_RESPONSIVENESS = 3

DEFAULT_KINT = 0.6
DEFAULT_KEXT = 0.02

# Maps responsiveness level (1=aggressive … 5=conservative) to TAU_CL_MIN_CYCLES.
# Indexed by responsiveness - 1.
RESPONSIVENESS_TO_TAU_CL_MIN: tuple[float, ...] = (1.5, 2.0, 3.0, 4.5, 6.0)

DEFAULT_OPTIONS: dict[str, str | int | bool | float] = {
    CONF_MINIMAL_ACTIVATION_DELAY: DEFAULT_MINIMAL_ACTIVATION_DELAY,
    CONF_MINIMAL_DEACTIVATION_DELAY: DEFAULT_MINIMAL_DEACTIVATION_DELAY,
    CONF_ADAPTIVE_TPI_DEBUG: DEFAULT_ADAPTIVE_TPI_DEBUG,
    CONF_RESPONSIVENESS: DEFAULT_RESPONSIVENESS,
    CONF_DEFAULT_KINT: DEFAULT_KINT,
    CONF_DEFAULT_KEXT: DEFAULT_KEXT,
}

PROP_FUNCTION_ADAPTIVE_TPI = "adaptive_tpi"

DATA_FACTORY_REGISTERED = "factory_registered"
DATA_SERVICES_REGISTERED = "services_registered"

SERVICE_RESET_LEARNING = "reset_learning"
