from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from .const import DOMAIN
from .hydros_hub import HydrosHub

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    if isinstance(entry_data, HydrosHub):
        entry_data = {"hub": entry_data}
        hass.data[DOMAIN][entry.entry_id] = entry_data

    hub: HydrosHub = entry_data["hub"]

    entities: list[HydrosDebugButton] = []
    for thing_id in hub.collective_ids:
        metadata = hub.get_collective_metadata(thing_id) or {}
        device_name = metadata.get("friendlyName") or metadata.get("thingName") or thing_id
        manufacturer = metadata.get("manufacturer") or "Hydros"
        model = metadata.get("thingType") or metadata.get("type")
        entities.append(
            HydrosDebugButton(
                hub=hub,
                thing_id=thing_id,
                device_info=DeviceInfo(
                    identifiers={(DOMAIN, thing_id)},
                    name=device_name,
                    manufacturer=manufacturer,
                    model=model,
                ),
            )
        )

    if entities:
        async_add_entities(entities)


class HydrosDebugButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, *, hub: HydrosHub, thing_id: str, device_info: DeviceInfo) -> None:
        self._hub = hub
        self._thing_id = thing_id
        self._device_info = device_info
        slug = slugify(f"{thing_id}-debug-sample")
        self._attr_unique_id = f"{hub.entry_id}-{thing_id}-{slug}"
        self._attr_name = "Debug Sample"

    @property
    def device_info(self) -> DeviceInfo:
        return self._device_info

    async def async_press(self) -> None:
        _LOGGER.debug("Hydros debug sample requested for %s", self._thing_id)
        await self._hub.async_collect_debug_sample(self._thing_id)
