"""Button platform: momentary (non-parameterized) commands on overridable
outputs, e.g. a feeder's manual "feed now" trigger with no dose-amount
argument. Also provides a "Resume Schedule" button for ``level``-type
outputs, since a number slider has no way to represent clearing an
override back to the device's own automatic schedule (``bool``/``flag``
outputs get this as the "Auto" option on their select entity instead).
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import HydrosDevice, is_manual_flow_rate
from .entity import HydrosEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    devices: dict[str, HydrosDevice] = hass.data[DOMAIN][entry.entry_id]["devices"]
    entities: list[ButtonEntity] = []

    for device in devices.values():
        for item in device.override_metadata:
            if is_manual_flow_rate(item):
                continue
            if item.type == "level":
                entities.append(HydrosResumeScheduleButton(device, item.key, item.name))
            if item.is_mode:
                continue
            for command_name, spec in item.commands.items():
                if not spec.is_parameterized:
                    label = spec.label or command_name
                    entities.append(
                        HydrosCommandButton(device, item.key, item.name, label, command_name)
                    )

    async_add_entities(entities)


class HydrosCommandButton(HydrosEntity, ButtonEntity):
    def __init__(
        self, device: HydrosDevice, override_key: str, output_name: str, label: str, command_name: str
    ) -> None:
        unique_key = device.unique_key(override_key, output_name)
        super().__init__(
            device,
            unique_id=f"{device.device_id}-{unique_key}-{command_name}",
            name=f"{output_name} {label}",
        )
        self._override_key = override_key
        self._command_name = command_name

    async def async_press(self) -> None:
        await self._device.async_send_command(self._override_key, self._command_name)


class HydrosResumeScheduleButton(HydrosEntity, ButtonEntity):
    """Clears a ``level``-type output's override, returning it to its own
    automatic schedule.
    """

    def __init__(self, device: HydrosDevice, override_key: str, output_name: str) -> None:
        super().__init__(
            device,
            unique_id=f"{device.device_id}-{device.unique_key(override_key, output_name)}-resume-schedule",
            name=f"{output_name} Resume Schedule",
        )
        self._override_key = override_key
        self._output_name = output_name

    async def async_press(self) -> None:
        await self._device.async_put_override(self._override_key, None, self._output_name)
