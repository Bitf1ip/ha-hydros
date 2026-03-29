from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_COLLECTIVES,
    CONF_PASSWORD,
    CONF_REGION,
    CONF_USERNAME,
    DEFAULT_REGION,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

try:
    from pyhydros import HydrosAPI, HydrosAPIError, HydrosAuthError
except ImportError as err:  # pragma: no cover
    HydrosAPI = None  # type: ignore[assignment]
    HydrosAPIError = Exception  # type: ignore[assignment]
    HydrosAuthError = Exception  # type: ignore[assignment]
    _IMPORT_ERROR = err
else:
    _IMPORT_ERROR = None

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


def _extract_thing_id(thing: dict[str, Any]) -> str | None:
    """Return the canonical thing identifier expected by the API."""
    for key in ("id", "thingId", "thing_id", "thingName"):
        value = thing.get(key)
        if isinstance(value, str):
            candidate = value.strip()
            if key == "thingName":
                candidate = "".join(candidate.split())
            if candidate:
                return candidate
    return None


def _fetch_collectives_sync(username: str, password: str, region: str) -> dict[str, str]:
    if _IMPORT_ERROR is not None:
        raise _IMPORT_ERROR

    api = HydrosAPI(username=username, password=password, region=region)
    api.authenticate()
    user_profile = api.get_user()
    selectable: dict[str, str] = {}
    for thing in user_profile.get("things", []):
        if not isinstance(thing, dict):
            continue

        thing_id = _extract_thing_id(thing)
        if not thing_id:
            continue

        thing_type = thing.get("thingType") or thing.get("type") or "Device"
        parent = thing.get("parent") or thing.get("parentThing")
        friendly = thing.get("friendlyName") or thing.get("thingName") or thing_id

        if thing_type == "Collective":
            selectable[thing_id] = friendly
            continue

        if parent:
            continue

        selectable[thing_id] = f"{friendly} (Standalone)"

    if not selectable:
        raise HydrosAPIError("No Hydros collectives or standalone devices found for this account")

    return selectable


class HydrosConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._username: str | None = None
        self._password: str | None = None
        self._region: str = DEFAULT_REGION
        self._collectives: dict[str, str] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=STEP_USER_DATA_SCHEMA,
                errors=errors,
            )

        username = user_input[CONF_USERNAME]
        password = user_input[CONF_PASSWORD]
        region = DEFAULT_REGION

        if _IMPORT_ERROR is not None:
            _LOGGER.error("Unable to import PyHydros during config flow: %s", _IMPORT_ERROR)
            errors["base"] = "cannot_connect"

        if errors:
            return self.async_show_form(
                step_id="user",
                data_schema=STEP_USER_DATA_SCHEMA,
                errors=errors,
            )

        await self.async_set_unique_id(username.lower())
        self._abort_if_unique_id_configured()

        try:
            collectives = await self.hass.async_add_executor_job(
                _fetch_collectives_sync, username, password, region
            )
        except HydrosAuthError:
            errors["base"] = "invalid_auth"
        except HydrosAPIError as err:
            _LOGGER.error("Hydros API error while fetching collectives: %s", err)
            errors["base"] = "cannot_connect"
        except Exception as err:  # pragma: no cover
            _LOGGER.exception("Unexpected Hydros error during config flow")
            errors["base"] = "unknown"

        if errors:
            return self.async_show_form(
                step_id="user",
                data_schema=STEP_USER_DATA_SCHEMA,
                errors=errors,
            )

        self._username = username
        self._password = password
        self._region = region
        self._collectives = collectives

        return await self.async_step_collectives()

    async def async_step_collectives(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if not self._collectives:
            return self.async_abort(reason="no_collectives")

        options = {
            thing_id: f"{name} ({thing_id})" if name != thing_id else thing_id
            for thing_id, name in self._collectives.items()
        }

        schema = vol.Schema(
            {vol.Required(CONF_COLLECTIVES): cv.multi_select(options)}
        )

        if user_input is None:
            return self.async_show_form(
                step_id="collectives",
                data_schema=schema,
                errors={},
            )

        selected = user_input.get(CONF_COLLECTIVES, [])
        if not selected:
            return self.async_show_form(
                step_id="collectives",
                data_schema=schema,
                errors={"base": "select_collective"},
            )

        title = self._build_entry_title(selected)
        data = {
            CONF_USERNAME: self._username,
            CONF_PASSWORD: self._password,
            CONF_REGION: self._region,
            CONF_COLLECTIVES: selected,
        }

        return self.async_create_entry(title=title, data=data)

    def _build_entry_title(self, selected: list[str]) -> str:
        names = [self._collectives.get(thing_id, thing_id) for thing_id in selected]
        deduped = []
        for name in names:
            if name not in deduped:
                deduped.append(name)
        if len(deduped) == 1:
            return deduped[0]
        return ", ".join(deduped)
