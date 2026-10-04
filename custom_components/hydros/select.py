"""Select platform: the device's operating ``mode`` override (override
metadata ``type == "mode"``), e.g. Normal/Feeding/Water Change -- plus
``bool``/``flag``-type overridable outputs, exposed as a discrete
On/Off/Auto (or Off/Auto) choice rather than a plain on/off switch, since
neither type is actually a two-state toggle: both can also be cleared back
to "Auto" (running on the device's own schedule), and ``flag`` outputs
(e.g. dosers) only accept ``true`` (shown as "Off") or ``null`` ("Auto"); the
API rejects ``false`` for them with HTTP 400.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from pyhydros2 import DeviceState

from .const import DOMAIN
from .coordinator import HydrosDevice
from .entity import HydrosEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    devices: dict[str, HydrosDevice] = hass.data[DOMAIN][entry.entry_id]["devices"]
    entities: list[SelectEntity] = []

    for device in devices.values():
        for item in device.override_metadata:
            if item.is_mode:
                entities.append(HydrosModeSelect(device, item.key, item.name, list(item.commands)))
            elif item.type == "bool":
                entities.append(HydrosOverrideSelect(device, item.key, item.name, ("Auto", "On", "Off")))
            elif item.type == "flag":
                entities.append(
                    HydrosOverrideSelect(device, item.key, item.name, ("Auto", "Off"), flag=True)
                )

    async_add_entities(entities)


class HydrosModeSelect(HydrosEntity, SelectEntity):
    def __init__(
        self, device: HydrosDevice, override_key: str, name: str, options: list[str]
    ) -> None:
        super().__init__(
            device,
            unique_id=f"{device.device_id}-{device.unique_key(override_key, name)}",
            name=name,
        )
        self._override_key = override_key
        self._attr_options = options

    @property
    def current_option(self) -> str | None:
        state: DeviceState = self.coordinator.data
        return state.mode

    async def async_select_option(self, option: str) -> None:
        await self._device.async_send_command(self._override_key, option)


class HydrosOverrideSelect(HydrosEntity, SelectEntity):
    """Tri-state (``bool``) or bi-state (``flag``) override control.

    ``flag``-type outputs (e.g. dosers) only accept ``true``/``null`` from
    the API -- sending ``false`` is rejected with HTTP 400. ``true`` means
    "Suspend Dosing", which the Hydros app shows as "Off"; ``null`` is "Auto".
    """

    def __init__(
        self,
        device: HydrosDevice,
        override_key: str,
        output_name: str,
        options: tuple[str, ...],
        *,
        flag: bool = False,
    ) -> None:
        super().__init__(
            device,
            unique_id=f"{device.device_id}-{device.unique_key(override_key, output_name)}",
            name=output_name,
        )
        self._override_key = override_key
        self._output_name = output_name
        self._flag = flag
        self._attr_options = list(options)

    @property
    def current_option(self) -> str | None:
        state: DeviceState = self.coordinator.data
        overridden = state.output_overridden(self._output_name)
        if overridden is None:
            return None
        if not overridden:
            return "Auto"
        if self._flag:
            return "Off"
        return "On" if state.output_on(self._output_name) else "Off"

    async def async_select_option(self, option: str) -> None:
        if self._flag:
            value = True if option == "Off" else None
        else:
            value = {"On": True, "Off": False, "Auto": None}[option]
        await self._device.async_put_override(self._override_key, value, self._output_name)
