"""Binary sensor platform: true on/off inputs (log series ``sensor_type ==
"bool"``) and a "Running" sensor per output. Tri-state float switches
(``sensor_type == "enum"``) are exposed as text sensors instead -- see sensor.py.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
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
    entities: list[BinarySensorEntity] = []

    for device in devices.values():
        state = device.state_coordinator.data
        for series in device.log_series:
            if series.sensor_type == "bool" and series.name in state.inputs:
                entities.append(HydrosBinaryInputSensor(device, series.name))

        for item in device.override_metadata:
            if item.is_mode or item.name not in state.outputs:
                continue
            entities.append(HydrosOutputRunningSensor(device, item.key, item.name))

    async_add_entities(entities)


class HydrosOutputRunningSensor(HydrosEntity, BinarySensorEntity):
    """On while the output is actually running (e.g. ATO pumping, doser dosing)."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, device: HydrosDevice, override_key: str, output_name: str) -> None:
        super().__init__(
            device,
            unique_id=f"{device.device_id}-{device.unique_key(override_key, output_name)}-running",
            name=f"{output_name} Running",
        )
        self._output_name = output_name

    @property
    def is_on(self) -> bool | None:
        state: DeviceState = self.coordinator.data
        output = state.outputs.get(self._output_name)
        if not isinstance(output, dict):
            return None
        # Dosers also carry a ``remaining`` field, assumed nonzero while a dose is in progress.
        return bool(output.get("valueState")) or bool(output.get("remaining"))


class HydrosBinaryInputSensor(HydrosEntity, BinarySensorEntity):
    def __init__(self, device: HydrosDevice, input_name: str) -> None:
        super().__init__(device, unique_id=f"{device.device_id}-{input_name}", name=input_name)
        self._input_name = input_name
        if "leak" in input_name.lower():
            self._attr_device_class = BinarySensorDeviceClass.MOISTURE

    @property
    def is_on(self) -> bool | None:
        state: DeviceState = self.coordinator.data
        return state.input_on(self._input_name)
