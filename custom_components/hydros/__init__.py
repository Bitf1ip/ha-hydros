from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS
from .hydros_hub import HydrosHub


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hub = HydrosHub(hass, entry)
    await hub.async_setup()

    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data[entry.entry_id] = {"hub": hub}

    if PLATFORMS:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = True
    if PLATFORMS:
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    domain_data = hass.data.get(DOMAIN)
    entry_data: dict[str, Any] | None = None
    if domain_data is not None:
        entry_data = domain_data.pop(entry.entry_id, None)

    sensor_manager = None
    binary_manager = None
    hub: HydrosHub | None = None
    if entry_data:
        sensor_manager = entry_data.get("sensor_manager")
        binary_manager = entry_data.get("binary_sensor_manager")
        hub = entry_data.get("hub")

    if sensor_manager:
        await sensor_manager.async_unload()

    if binary_manager:
        await binary_manager.async_unload()

    if hub:
        await hub.async_unload()

    if domain_data is not None and not domain_data:
        hass.data.pop(DOMAIN, None)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
