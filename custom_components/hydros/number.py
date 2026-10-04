"""Number platform: ``level``-type overridable outputs (variable-speed
pumps, dimmed lights -- shown as a 0-100% slider, scaled to each entry's
own ``min``/``max`` from override metadata -- NOT a universal 0-10000
raw scale; e.g. a doser's manual flow-rate channel can be min=1/max=1000),
plus parameterized commands on any overridable output (e.g. a doser's
"dose" command, which takes an explicit amount rather than being a plain
on/off; per the API schema, named commands are orthogonal to an entry's
override ``type`` and can appear alongside a ``level`` entry's numeric
range too).
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from pyhydros2 import DeviceState

from .const import DOMAIN
from .coordinator import HydrosDevice, is_manual_flow_rate
from .entity import HydrosEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    devices: dict[str, HydrosDevice] = hass.data[DOMAIN][entry.entry_id]["devices"]
    entities: list[NumberEntity] = []

    for device in devices.values():
        for item in device.override_metadata:
            if is_manual_flow_rate(item):
                continue
            if item.type == "level":
                entities.append(
                    HydrosLevelNumber(device, item.key, item.name, item.min, item.max)
                )
            if not item.is_mode:
                for command_name, spec in item.commands.items():
                    if spec.is_parameterized:
                        entities.append(
                            HydrosCommandNumber(
                                device, item.key, item.name, command_name, spec
                            )
                        )

    async_add_entities(entities)


class HydrosLevelNumber(HydrosEntity, NumberEntity):
    """A ``level``-type override, shown as a 0-100% slider.

    Percent is computed relative to this entry's own ``min``/``max`` (from
    override metadata), not a universal 0-10000 raw scale -- those differ
    per entry (e.g. a doser's manual flow-rate channel is min=1/max=1000,
    while a typical output is min=0/max=10000).
    """

    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        device: HydrosDevice,
        override_key: str,
        output_name: str,
        min_value: int | None,
        max_value: int | None,
    ) -> None:
        super().__init__(
            device,
            unique_id=f"{device.device_id}-{device.unique_key(override_key, output_name)}",
            name=output_name,
        )
        self._override_key = override_key
        self._output_name = output_name
        self._min = min_value if min_value is not None else 0
        self._max = max_value if max_value is not None else 10000

    @property
    def native_value(self) -> float | None:
        state: DeviceState = self.coordinator.data
        output = state.outputs.get(self._output_name)
        raw = output.get("valueState") if isinstance(output, dict) else None
        if not isinstance(raw, (int, float)):
            return None
        span = self._max - self._min
        if span == 0:
            return 0.0
        return (raw - self._min) / span * 100

    async def async_set_native_value(self, value: float) -> None:
        raw = round(self._min + value / 100 * (self._max - self._min))
        await self._device.async_put_override(self._override_key, raw, self._output_name)


class HydrosCommandNumber(HydrosEntity, NumberEntity):
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        device: HydrosDevice,
        override_key: str,
        output_name: str,
        command_name: str,
        spec,
    ) -> None:
        label = spec.label or command_name
        super().__init__(
            device,
            unique_id=f"{device.device_id}-{device.unique_key(override_key, output_name)}-{command_name}",
            name=f"{output_name} {label}",
        )
        self._override_key = override_key
        self._command_name = command_name
        self._attr_native_min_value = spec.arg_min if spec.arg_min is not None else 0
        self._attr_native_max_value = spec.arg_max if spec.arg_max is not None else 100
        self._attr_native_unit_of_measurement = spec.arg_unit
        # These commands are momentary actions (e.g. "dose N units"); there's
        # no persistent "current value" to read back, so this behaves as a
        # write-only setpoint that always resets to its minimum.
        self._attr_native_step = 1

    @property
    def native_value(self) -> float:
        return self._attr_native_min_value

    async def async_set_native_value(self, value: float) -> None:
        await self._device.async_send_command(self._override_key, self._command_name, value=round(value))
