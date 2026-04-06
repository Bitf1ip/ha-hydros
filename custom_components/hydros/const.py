from __future__ import annotations

DOMAIN = "hydros"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_REGION = "region"
CONF_COLLECTIVES = "collectives"
CONF_ENABLE_REMOTE_CONTROL = "enable_remote_control"
CONF_ACCEPT_REMOTE_CONTROL_DISCLAIMER = "accept_remote_control_disclaimer"
DEFAULT_REGION = "us-west-2"
DEFAULT_WATCHDOG_INACTIVITY = 5

PLATFORMS: list[str] = ["sensor", "binary_sensor", "button", "select"]

SIGNAL_COLLECTIVE_UPDATED = "hydros_collective_updated_{entry}_{thing}"
SIGNAL_CONFIG_UPDATED = "hydros_config_updated_{entry}_{thing}"
