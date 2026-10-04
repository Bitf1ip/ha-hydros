from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from pyhydros2 import (
    DeviceStatePoller,
    HydrosAPIError,
    HydrosAuthError,
    HydrosClient,
    HydrosForbiddenError,
    SessionResponse,
)

from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_KEY,
    CONF_DEVICES,
    CONF_NAME,
    CONF_PROVIDER_KEY,
    CONF_USERNAME,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import HydrosDevice, HydrosDosingCoordinator, HydrosStateCoordinator


def _session_store(hass: HomeAssistant, device_id: str) -> Store[dict[str, Any]]:
    return Store(hass, 1, f"{DOMAIN}.session.{device_id}")


def _session_to_dict(session: SessionResponse) -> dict[str, Any]:
    return {
        "pollUrl": session.poll_url,
        "pollToken": session.poll_token,
        "durationSeconds": session.duration_seconds,
        "pollIntervalSeconds": session.poll_interval_seconds,
        "expiresAt": session.expires_at.isoformat(),
    }


async def _async_build_poller(
    hass: HomeAssistant, client: HydrosClient, device_id: str
) -> DeviceStatePoller:
    """Reuse a stored polling session: starting one is capped at 5/hour per
    device, which reloads and restarts would otherwise exhaust.
    """
    store = _session_store(hass, device_id)
    initial_session: SessionResponse | None = None
    if cached := await store.async_load():
        try:
            initial_session = SessionResponse.from_dict(cached)
        except (KeyError, TypeError, ValueError):
            initial_session = None

    async def _save(session: SessionResponse) -> None:
        await store.async_save(_session_to_dict(session))

    return DeviceStatePoller(client, initial_session=initial_session, on_session_change=_save)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if CONF_USERNAME in entry.data and CONF_DEVICES not in entry.data:
        # Legacy (pyhydros/MQTT) config entry: the public API uses a
        # completely different credential model (per-device keys instead of
        # an account login), so there's nothing to silently carry over. This
        # triggers Home Assistant's standard reauth flow on this same entry.
        raise ConfigEntryAuthFailed(
            "Hydros now requires a provider/device API key pair for each "
            "device; re-authenticate to continue."
        )

    session = async_get_clientsession(hass)
    devices: dict[str, HydrosDevice] = {}

    for device_data in entry.data.get(CONF_DEVICES, []):
        device_id = device_data[CONF_DEVICE_ID]
        name = device_data.get(CONF_NAME) or device_id
        client = HydrosClient(
            device_data[CONF_PROVIDER_KEY],
            device_data[CONF_DEVICE_KEY],
            session=session,
        )

        try:
            device = await client.get_device()
            override_metadata = await client.get_override_metadata()
            log_series_discovery = await client.discover_log_series()
        except (HydrosAuthError, HydrosForbiddenError) as err:
            raise ConfigEntryAuthFailed(f"Hydros rejected the API keys for {name}: {err}") from err
        except HydrosAPIError as err:
            raise ConfigEntryNotReady(f"Hydros setup failed for {name}: {err}") from err

        poller = await _async_build_poller(hass, client, device_id)
        state_coordinator = HydrosStateCoordinator(hass, poller, name)
        await state_coordinator.async_config_entry_first_refresh()

        doser_names = [item.name for item in override_metadata if "dose" in item.commands]
        dosing_coordinator = HydrosDosingCoordinator(hass, client, doser_names, name)
        # Dosing totals are non-essential; a logs outage shouldn't block the device's controls.
        await dosing_coordinator.async_refresh()

        devices[device_id] = HydrosDevice(
            device_id=device_id,
            name=name,
            device_type=device.type,
            client=client,
            state_coordinator=state_coordinator,
            dosing_coordinator=dosing_coordinator,
            override_metadata=override_metadata,
            log_series=log_series_discovery.series,
        )

    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data[entry.entry_id] = {"devices": devices}

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        domain_data = hass.data.get(DOMAIN, {})
        domain_data.pop(entry.entry_id, None)
        if not domain_data:
            hass.data.pop(DOMAIN, None)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the stored polling sessions, which hold a bearer poll token."""
    for device_data in entry.data.get(CONF_DEVICES, []):
        await _session_store(hass, device_data[CONF_DEVICE_ID]).async_remove()
