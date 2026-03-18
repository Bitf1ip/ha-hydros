from __future__ import annotations

DOMAIN = "hydros"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_REGION = "region"
CONF_COLLECTIVES = "collectives"
DEFAULT_REGION = "us-west-2"
DEFAULT_WATCHDOG_INACTIVITY = 5

PLATFORMS: list[str] = ["sensor", "binary_sensor", "button"]

SIGNAL_COLLECTIVE_UPDATED = "hydros_collective_updated_{entry}_{thing}"
SIGNAL_CONFIG_UPDATED = "hydros_config_updated_{entry}_{thing}"
