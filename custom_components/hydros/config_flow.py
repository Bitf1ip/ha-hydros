from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from pyhydros2 import (
    HydrosAPIError,
    HydrosAuthError,
    HydrosClient,
    HydrosConfigError,
    HydrosForbiddenError,
    LogSeriesInfo,
    OverrideMetadataEntry,
)

from .const import (
    CONF_COLLECTIVES,
    CONF_DEVICE_ID,
    CONF_DEVICE_KEY,
    CONF_DEVICES,
    CONF_NAME,
    CONF_PROVIDER_KEY,
    DOMAIN,
)
from .migration import async_migrate_device_entities

_LOGGER = logging.getLogger(__name__)

_SECRET = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

STEP_DEVICE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PROVIDER_KEY): _SECRET,
        vol.Required(CONF_DEVICE_KEY): _SECRET,
        vol.Optional(CONF_NAME): str,
    }
)

# Blank fields keep the stored key, so only the rejected one has to be re-entered.
STEP_REAUTH_KEYS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROVIDER_KEY): _SECRET,
        vol.Optional(CONF_DEVICE_KEY): _SECRET,
    }
)

STEP_ADD_ANOTHER_SCHEMA = vol.Schema({vol.Required("add_another", default=False): bool})


async def _async_validate_device(
    hass: HomeAssistant, user_input: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate a provider/device key pair and return a stored device dict."""
    provider_key = user_input[CONF_PROVIDER_KEY].strip()
    device_key = user_input[CONF_DEVICE_KEY].strip()
    client = HydrosClient(provider_key, device_key, session=async_get_clientsession(hass))
    device = await client.get_device()

    return {
        CONF_PROVIDER_KEY: provider_key,
        CONF_DEVICE_KEY: device_key,
        CONF_DEVICE_ID: device.device_id,
        CONF_NAME: (user_input.get(CONF_NAME) or "").strip() or device.friendly_name,
    }


async def _async_try_validate(
    hass: HomeAssistant, user_input: Mapping[str, Any], errors: dict[str, str]
) -> dict[str, Any] | None:
    """``_async_validate_device``, recording a form error instead of raising."""
    try:
        return await _async_validate_device(hass, user_input)
    except (HydrosAuthError, HydrosForbiddenError, HydrosConfigError) as err:
        _LOGGER.warning("Hydros rejected the API keys: %s", err)
        errors["base"] = "invalid_auth"
    except HydrosAPIError as err:
        _LOGGER.warning("Could not reach Hydros (status=%s): %s", err.status_code, err)
        errors["base"] = "cannot_connect"
    except Exception:  # pragma: no cover
        _LOGGER.exception("Unexpected error validating Hydros device")
        errors["base"] = "unknown"
    return None


@dataclass
class _LegacyMigration:
    thing_id: str
    device: dict[str, Any]
    override_metadata: list[OverrideMetadataEntry]
    log_series: list[LogSeriesInfo]


class HydrosConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._pending_devices: list[dict[str, Any]] = []
        # Reauth of a current entry: its devices, with keys replaced as the user confirms each.
        self._reauth_devices: list[dict[str, Any]] = []
        self._reauth_index = 0
        # Reauth of a legacy (pyhydros/MQTT) entry: one migration per old thing_id.
        self._legacy_thing_ids: list[str] = []
        self._legacy_migrations: list[_LegacyMigration] = []

    def _configured_device_ids(self) -> set[str]:
        """Device ids already claimed by this flow or by another config entry."""
        ids = {device[CONF_DEVICE_ID] for device in self._pending_devices}
        ids.update(migration.device[CONF_DEVICE_ID] for migration in self._legacy_migrations)
        reauth_entry_id = self.context.get("entry_id")
        for entry in self._async_current_entries(include_ignore=False):
            if entry.entry_id != reauth_entry_id:
                ids.update(device[CONF_DEVICE_ID] for device in entry.data.get(CONF_DEVICES, []))
        return ids

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            device = await _async_try_validate(self.hass, user_input, errors)
            if device is not None and device[CONF_DEVICE_ID] in self._configured_device_ids():
                errors["base"] = "already_configured"
            elif device is not None:
                if not self._pending_devices:
                    await self.async_set_unique_id(device[CONF_DEVICE_ID])
                self._pending_devices.append(device)
                return await self.async_step_add_another()

        return self.async_show_form(
            step_id="user", data_schema=STEP_DEVICE_DATA_SCHEMA, errors=errors
        )

    async def async_step_add_another(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="add_another", data_schema=STEP_ADD_ANOTHER_SCHEMA
            )

        if user_input.get("add_another"):
            return await self.async_step_user()

        title = ", ".join(d[CONF_NAME] for d in self._pending_devices)
        return self.async_create_entry(
            title=title, data={CONF_DEVICES: self._pending_devices}
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> FlowResult:
        if CONF_DEVICES in entry_data:
            self._reauth_devices = [dict(device) for device in entry_data[CONF_DEVICES]]
            self._reauth_index = 0
            return await self.async_step_reauth_keys()
        return await self._async_start_legacy_reauth(entry_data)

    # ------------------------------------------------------------------
    # Reauth of a current entry: replace rejected keys, device by device.
    # ------------------------------------------------------------------

    async def async_step_reauth_keys(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        stored = self._reauth_devices[self._reauth_index]

        if user_input is not None:
            candidate = {
                CONF_PROVIDER_KEY: user_input.get(CONF_PROVIDER_KEY) or stored[CONF_PROVIDER_KEY],
                CONF_DEVICE_KEY: user_input.get(CONF_DEVICE_KEY) or stored[CONF_DEVICE_KEY],
                CONF_NAME: stored.get(CONF_NAME),
            }
            device = await _async_try_validate(self.hass, candidate, errors)
            if device is not None and device[CONF_DEVICE_ID] != stored[CONF_DEVICE_ID]:
                errors["base"] = "wrong_device"
            elif device is not None:
                stored[CONF_PROVIDER_KEY] = device[CONF_PROVIDER_KEY]
                stored[CONF_DEVICE_KEY] = device[CONF_DEVICE_KEY]
                self._reauth_index += 1
                if self._reauth_index < len(self._reauth_devices):
                    return await self.async_step_reauth_keys()
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(), data_updates={CONF_DEVICES: self._reauth_devices}
                )

        return self.async_show_form(
            step_id="reauth_keys",
            data_schema=STEP_REAUTH_KEYS_SCHEMA,
            errors=errors,
            description_placeholders={
                "device": stored.get(CONF_NAME) or stored[CONF_DEVICE_ID]
            },
        )

    # ------------------------------------------------------------------
    # Reauth of a legacy (pyhydros/MQTT) entry: migrate to per-device keys.
    # ------------------------------------------------------------------

    async def _async_start_legacy_reauth(self, entry_data: Mapping[str, Any]) -> FlowResult:
        self._legacy_thing_ids = list(entry_data.get(CONF_COLLECTIVES, []))
        self._legacy_migrations = []
        if not self._legacy_thing_ids:
            _LOGGER.error(
                "Hydros reauth: entry %s has no '%s' to migrate -- aborting",
                self.context.get("entry_id"),
                CONF_COLLECTIVES,
            )
            return self.async_abort(reason="no_collectives")
        return await self.async_step_reauth_device()

    async def async_step_reauth_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        thing_id = self._legacy_thing_ids[len(self._legacy_migrations)]

        if user_input is not None:
            device = await _async_try_validate(self.hass, user_input, errors)
            if device is not None and device[CONF_DEVICE_ID] in self._configured_device_ids():
                errors["base"] = "already_configured"
            elif device is not None:
                client = HydrosClient(
                    device[CONF_PROVIDER_KEY],
                    device[CONF_DEVICE_KEY],
                    session=async_get_clientsession(self.hass),
                )
                try:
                    override_metadata = await client.get_override_metadata()
                    log_series = (await client.discover_log_series()).series
                except HydrosAPIError as err:
                    _LOGGER.warning("Could not read Hydros device metadata: %s", err)
                    errors["base"] = "cannot_connect"
                else:
                    self._legacy_migrations.append(
                        _LegacyMigration(thing_id, device, override_metadata, log_series)
                    )
                    if len(self._legacy_migrations) < len(self._legacy_thing_ids):
                        return await self.async_step_reauth_device()
                    return await self._async_finish_legacy_reauth()

        return self.async_show_form(
            step_id="reauth_device",
            data_schema=STEP_DEVICE_DATA_SCHEMA,
            errors=errors,
            description_placeholders={"thing_id": thing_id},
        )

    async def _async_finish_legacy_reauth(self) -> FlowResult:
        reauth_entry = self._get_reauth_entry()
        for migration in self._legacy_migrations:
            await async_migrate_device_entities(
                self.hass,
                reauth_entry,
                old_thing_id=migration.thing_id,
                new_device_id=migration.device[CONF_DEVICE_ID],
                override_metadata=migration.override_metadata,
                log_series=migration.log_series,
            )

        return self.async_update_reload_and_abort(
            reauth_entry,
            data={CONF_DEVICES: [migration.device for migration in self._legacy_migrations]},
        )
