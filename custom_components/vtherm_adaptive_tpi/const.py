"""Constants for the vtherm_adaptive_tpi integration."""

from __future__ import annotations

DOMAIN = "vtherm_adaptive_tpi"
NAME = "Versatile Thermostat Adaptive TPI"

CONF_TARGET_VTHERM = "target_vtherm_unique_id"
CONF_MINIMAL_ACTIVATION_DELAY = "minimal_activation_delay"
CONF_MINIMAL_DEACTIVATION_DELAY = "minimal_deactivation_delay"
CONF_ADAPTIVE_TPI_DEBUG = "adaptive_tpi_debug"

DEFAULT_MINIMAL_ACTIVATION_DELAY = 0
DEFAULT_MINIMAL_DEACTIVATION_DELAY = 0
DEFAULT_ADAPTIVE_TPI_DEBUG = False

DEFAULT_KINT = 0.6
DEFAULT_KEXT = 0.01

DEFAULT_OPTIONS: dict[str, str | int | bool] = {
    CONF_MINIMAL_ACTIVATION_DELAY: DEFAULT_MINIMAL_ACTIVATION_DELAY,
    CONF_MINIMAL_DEACTIVATION_DELAY: DEFAULT_MINIMAL_DEACTIVATION_DELAY,
    CONF_ADAPTIVE_TPI_DEBUG: DEFAULT_ADAPTIVE_TPI_DEBUG,
}

PROP_FUNCTION_ADAPTIVE_TPI = "adaptive_tpi"

DATA_FACTORY_REGISTERED = "factory_registered"
DATA_SERVICES_REGISTERED = "services_registered"

SERVICE_RESET_LEARNING = "reset_learning"
