from __future__ import annotations

DOMAIN = "hydros"

# -- New (pyhydros2 / public REST API) config schema -------------------------
# entry.data[CONF_DEVICES] is a list of per-device credential dicts:
#   {CONF_PROVIDER_KEY: str, CONF_DEVICE_KEY: str, CONF_DEVICE_ID: str, CONF_NAME: str}
# One config entry may hold several devices (mirrors the old integration's
# ability to group several collectives under one login), but each device is
# authenticated independently since the public API has no account concept.
CONF_DEVICES = "devices"
CONF_PROVIDER_KEY = "provider_key"
CONF_DEVICE_KEY = "device_key"
CONF_DEVICE_ID = "device_id"
CONF_NAME = "name"

API_KEYS_HELP_URL = "https://github.com/Bitf1ip/ha-hydros#getting-your-api-keys"
PLATFORMS: list[str] = ["sensor", "binary_sensor", "number", "select", "button"]

STATE_UPDATE_INTERVAL_SECONDS = 7
DOSING_UPDATE_INTERVAL_SECONDS = 300

# -- Legacy (pre-2.0, pyhydros/MQTT) config schema ---------------------------
# Kept only so __init__.py can recognize an old-style config entry and
# trigger a reauth flow; never written by the new config flow.
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_REGION = "region"
CONF_COLLECTIVES = "collectives"
CONF_ENABLE_REMOTE_CONTROL = "enable_remote_control"
CONF_ACCEPT_REMOTE_CONTROL_DISCLAIMER = "accept_remote_control_disclaimer"
