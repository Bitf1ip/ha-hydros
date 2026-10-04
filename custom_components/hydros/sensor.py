"""Sensor platform: read-only values derived from device state, dosing
totals, and discovered log series.

Entity typing has one fundamental limitation compared to the old
MQTT-based integration: the public API does not classify *why* a value
exists (no ``senseMode``/``probeMode``/output ``type``/``family`` config
blob). Analog input units/device classes below are therefore a best-effort
guess from the input's name, purely cosmetic -- the underlying numeric
value is always correct regardless of whether the guess matches.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfVolume,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from pyhydros2 import DeviceState, units

from .const import DOMAIN
from .coordinator import HydrosDevice, HydrosDosingCoordinator
from .entity import HydrosEntity

# Best-effort cosmetic unit/device_class guess for analog inputs, since the
# public API doesn't classify sensor purpose. (name_substring, unit, device_class)
# pH keeps its "pH" unit with no device class (HA's pH class forbids a unit), so
# long-term statistics recorded by earlier versions stay compatible.
_ANALOG_NAME_HINTS: tuple[tuple[str, str | None, SensorDeviceClass | None], ...] = (
    ("temp", UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE),
    ("ph", "pH", None),
    ("orp", UnitOfElectricPotential.MILLIVOLT, None),
    ("conduct", "µS/cm", None),
    ("salin", "ppt", None),
    ("tds", "ppm", None),
    ("flow", UnitOfVolumeFlowRate.LITERS_PER_HOUR, SensorDeviceClass.VOLUME_FLOW_RATE),
)

# Same guess keyed by the log series' firmware type code, seen on real hardware.
_ANALOG_TYPE_HINTS: dict[str, tuple[str | None, SensorDeviceClass | None]] = {
    "Tmp": (UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE),
    "pH": ("pH", None),
    "DKH": ("dKH", None),
    "Flo": (UnitOfVolumeFlowRate.LITERS_PER_HOUR, SensorDeviceClass.VOLUME_FLOW_RATE),
}

_OUTPUT_METRICS: tuple[tuple[str, str, str, SensorDeviceClass, str], ...] = (
    # (state field, accessor name, unit, device_class, display label)
    ("voltageI", "output_voltage_volts", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE, "Voltage"),
    ("current", "output_current_amps", UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT, "Current"),
    ("powerI", "output_power_watts", UnitOfPower.WATT, SensorDeviceClass.POWER, "Power"),
    ("frequency", "output_frequency_hz", UnitOfFrequency.HERTZ, SensorDeviceClass.FREQUENCY, "Frequency"),
)

# The state document's timestamps carry a timezone abbreviation, not an offset.
_TZ_OFFSET_HOURS = {
    "UTC": 0, "GMT": 0,
    "EST": -5, "EDT": -4, "CST": -6, "CDT": -5,
    "MST": -7, "MDT": -6, "PST": -8, "PDT": -7,
}


def _parse_device_time(value: Any) -> datetime | None:
    """Parse e.g. ``"2026-04-12 13:13:58 EDT"``; None for unknown abbreviations."""
    if not isinstance(value, str):
        return None
    stamp, _, abbreviation = value.rpartition(" ")
    offset = _TZ_OFFSET_HOURS.get(abbreviation)
    if offset is None:
        return None
    try:
        naive = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=timezone(timedelta(hours=offset)))


class _ControllerField(NamedTuple):
    path: tuple[str, ...]
    label: str
    convert: Callable[[Any], Any] | None = None
    unit: str | None = None
    device_class: SensorDeviceClass | None = None


# Per-node entries of the state document's ``health`` section.
_CONTROLLER_FIELDS: tuple[_ControllerField, ...] = (
    _ControllerField(
        ("busPower", "voltageI"), "Bus Voltage", units.centivolts_to_volts,
        UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE,
    ),
    _ControllerField(
        ("busPower", "current"), "Bus Current", units.milliamps_to_amps,
        UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT,
    ),
    _ControllerField(
        ("temperatureI",), "Temperature", units.eighths_celsius_to_celsius,
        UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE,
    ),
    _ControllerField(("time",), "Last Report", _parse_device_time, None, SensorDeviceClass.TIMESTAMP),
    _ControllerField(("bootTime",), "Boot Time", _parse_device_time, None, SensorDeviceClass.TIMESTAMP),
    _ControllerField(("wifiState",), "WiFi State"),
    _ControllerField(("sdCardStatus",), "SD Card Status"),
    _ControllerField(("selfTestRunCount",), "Self Tests Run"),
    _ControllerField(("selfTestPassCount",), "Self Tests Passed"),
)


def _dig(node: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = node
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _guess_analog_unit_and_class(
    name: str, log_type: str | None = None
) -> tuple[str | None, SensorDeviceClass | None]:
    # The firmware log type (e.g. "DKH") is more reliable than the user-chosen name.
    if log_type in _ANALOG_TYPE_HINTS:
        return _ANALOG_TYPE_HINTS[log_type]
    lowered = name.lower()
    for hint, unit, device_class in _ANALOG_NAME_HINTS:
        if hint in lowered:
            return unit, device_class
    return None, None


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    devices: dict[str, HydrosDevice] = hass.data[DOMAIN][entry.entry_id]["devices"]
    entities: list[SensorEntity] = []

    for device in devices.values():
        state = device.state_coordinator.data

        entities.append(HydrosAlertsSensor(device))
        if state.firmware_version is not None:
            entities.append(HydrosDiagnosticSensor(device, "firmware_version", "Firmware Version"))
        if state.collective_status is not None:
            entities.append(HydrosDiagnosticSensor(device, "collective_status", "Collective Status"))
        if state.temperature_celsius is not None:
            entities.append(HydrosTemperatureSensor(device))
        entities.append(HydrosModeEndsSensor(device))

        for node_id, node in state.health.items():
            if not isinstance(node, dict):
                continue
            # via_device_id needs the parent's registry id, so register it first.
            parent_id = dr.async_get(hass).async_get_or_create(
                config_entry_id=entry.entry_id, **device.device_info
            ).id
            for field in _CONTROLLER_FIELDS:
                if _dig(node, field.path) is not None:
                    entities.append(HydrosControllerSensor(device, node_id, field, parent_id))
        for item in device.override_metadata:
            if "dose" in item.commands:
                entities.append(HydrosDosedTodaySensor(device, item.key, item.name))

        power_output_names: list[str] = []
        for item in device.override_metadata:
            output = state.outputs.get(item.name, {})
            if not isinstance(output, dict):
                continue
            for field, accessor, unit, device_class, label in _OUTPUT_METRICS:
                if field in output:
                    entities.append(
                        HydrosOutputMetricSensor(
                            device, item.key, item.name, accessor, unit, device_class, label
                        )
                    )
            if "powerI" in output:
                power_output_names.append(item.name)
            if "reservoir" in output:
                entities.append(HydrosReservoirSensor(device, item.key, item.name))

        # The old MQTT integration surfaced one hardware-reported "XP8 Total
        # Power" value per power-monitoring strip. The public API has no
        # equivalent grouping of which outputs share a physical strip, so
        # this sums every output currently reporting a power reading
        # instead -- a software approximation, not a hardware meter. Skipped
        # when there's only one such output since it would just duplicate
        # that output's own "Power" sensor.
        if len(power_output_names) >= 2:
            entities.append(HydrosTotalPowerSensor(device, power_output_names))

        for series in device.log_series:
            if series.sensor_type == "analog" and series.name in state.inputs:
                entities.append(HydrosAnalogInputSensor(device, series.name, series.type))
            elif series.sensor_type == "enum" and series.name in state.inputs:
                entities.append(HydrosEnumInputSensor(device, series.name))
            elif series.sensor_type not in ("bool", "analog", "enum") and series.name in state.inputs:
                # Covers ``event``/``unknown`` (and any future sensorType the
                # API adds) -- surface the raw payload rather than silently
                # dropping the input entirely.
                entities.append(HydrosRawInputSensor(device, series.name))

    async_add_entities(entities)


class HydrosAlertsSensor(HydrosEntity, SensorEntity):
    def __init__(self, device: HydrosDevice) -> None:
        super().__init__(device, unique_id=f"{device.device_id}-alerts", name="Alerts")

    @property
    def native_value(self) -> str:
        state: DeviceState = self.coordinator.data
        return state.alert_summary()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state: DeviceState = self.coordinator.data
        return {"alerts": state.alerts()}


class HydrosDiagnosticSensor(HydrosEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, device: HydrosDevice, attribute: str, name: str) -> None:
        super().__init__(device, unique_id=f"{device.device_id}-{attribute}", name=name)
        self._attribute = attribute

    @property
    def native_value(self) -> Any:
        state: DeviceState = self.coordinator.data
        return getattr(state, self._attribute)


class HydrosControllerSensor(HydrosEntity, SensorEntity):
    """One health field of a controller node, grouped under its own child device."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, device: HydrosDevice, node_id: str, field: _ControllerField, parent_device_id: str
    ) -> None:
        slug = "-".join(field.path)
        super().__init__(
            device, unique_id=f"{device.device_id}-health-{node_id}-{slug}", name=field.label
        )
        self._node_id = node_id
        self._parent_device_id = parent_device_id
        self._field = field
        self._attr_native_unit_of_measurement = field.unit
        self._attr_device_class = field.device_class
        if field.device_class in (
            SensorDeviceClass.VOLTAGE,
            SensorDeviceClass.CURRENT,
            SensorDeviceClass.TEMPERATURE,
        ):
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._device.device_id}-{self._node_id}")},
            name=f"Controller {self._node_id}",
            manufacturer="CoralVue Hydros",
            via_device_id=self._parent_device_id,
        )

    @property
    def native_value(self) -> Any:
        state: DeviceState = self.coordinator.data
        node = state.health.get(self._node_id)
        if not isinstance(node, dict):
            return None
        value = _dig(node, self._field.path)
        if value is None or self._field.convert is None:
            return value
        return self._field.convert(value)


class HydrosModeEndsSensor(HydrosEntity, SensorEntity):
    """When the current timed mode (e.g. Water Change) ends and the device returns to Normal."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, device: HydrosDevice) -> None:
        super().__init__(device, unique_id=f"{device.device_id}-mode-ends", name="Mode Ends")

    @property
    def native_value(self) -> datetime | None:
        state: DeviceState = self.coordinator.data
        remaining = state.mode_timeout_seconds
        if remaining is None:
            return None
        # Anchoring to the snapshot's own clock keeps the end time steady across polls.
        snapshot_time = _parse_device_time(state.get("time")) or dt_util.utcnow()
        return (snapshot_time + timedelta(seconds=remaining)).replace(microsecond=0)


class HydrosTemperatureSensor(HydrosEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, device: HydrosDevice) -> None:
        super().__init__(device, unique_id=f"{device.device_id}-temperature", name="Temperature")

    @property
    def native_value(self) -> float | None:
        state: DeviceState = self.coordinator.data
        return state.temperature_celsius


class HydrosOutputMetricSensor(HydrosEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        device: HydrosDevice,
        override_key: str,
        output_name: str,
        accessor: str,
        unit: str,
        device_class: SensorDeviceClass,
        label: str,
    ) -> None:
        super().__init__(
            device,
            unique_id=f"{device.device_id}-{device.unique_key(override_key, output_name)}-{accessor}",
            name=(
                output_name
                if output_name.lower().endswith(label.lower())
                else f"{output_name} {label}"
            ),
        )
        self._output_name = output_name
        self._accessor = accessor
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class

    @property
    def native_value(self) -> float | None:
        state: DeviceState = self.coordinator.data
        return getattr(state, self._accessor)(self._output_name)


class HydrosTotalPowerSensor(HydrosEntity, SensorEntity):
    """Sum of every output currently reporting a power reading.

    See the comment in ``async_setup_entry`` -- this is a software
    approximation of the old integration's hardware-reported XP8 strip
    total, not a direct replacement of it.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, device: HydrosDevice, output_names: list[str]) -> None:
        super().__init__(device, unique_id=f"{device.device_id}-total-power", name="Total Power")
        self._output_names = output_names

    @property
    def native_value(self) -> float:
        state: DeviceState = self.coordinator.data
        total = 0.0
        for name in self._output_names:
            value = state.output_power_watts(name)
            if value is not None:
                total += value
        return round(total, 2)


class HydrosReservoirSensor(HydrosEntity, SensorEntity):
    """Remaining liquid in a dosing pump's reservoir, as reported by firmware.

    Firmware computes this from a reservoir capacity calibrated in the
    manufacturer's app; pyhydros2 passes the raw ``reservoir`` output field
    through unchanged (see ``output_reservoir_ml``).
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.VOLUME_STORAGE
    _attr_native_unit_of_measurement = UnitOfVolume.MILLILITERS

    def __init__(self, device: HydrosDevice, override_key: str, output_name: str) -> None:
        super().__init__(
            device,
            unique_id=f"{device.device_id}-{device.unique_key(override_key, output_name)}-reservoir",
            name=f"{output_name} Reservoir Remaining",
        )
        self._output_name = output_name

    @property
    def native_value(self) -> float | None:
        state: DeviceState = self.coordinator.data
        return state.output_reservoir_ml(self._output_name)


class HydrosDosedTodaySensor(CoordinatorEntity[HydrosDosingCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = UnitOfVolume.MILLILITERS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, device: HydrosDevice, override_key: str, output_name: str) -> None:
        super().__init__(device.dosing_coordinator)
        self._device = device
        self._output_name = output_name
        unique_key = device.unique_key(override_key, output_name)
        self._attr_unique_id = f"{device.device_id}-{unique_key}-dosed-today"
        self._attr_name = f"{output_name} Dosed Today"

    @property
    def device_info(self):
        return self._device.device_info

    @property
    def native_value(self) -> float | None:
        return (self.coordinator.data or {}).get(self._output_name)


class HydrosAnalogInputSensor(HydrosEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, device: HydrosDevice, input_name: str, log_type: str | None = None) -> None:
        super().__init__(device, unique_id=f"{device.device_id}-{input_name}", name=input_name)
        self._input_name = input_name
        unit, device_class = _guess_analog_unit_and_class(input_name, log_type)
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class

    @property
    def native_value(self) -> Any:
        state: DeviceState = self.coordinator.data
        payload = state.inputs.get(self._input_name, {})
        if not isinstance(payload, dict):
            return None
        # Probe inputs (pH etc.) report ``probeValue``; other analog inputs ``senseValue``.
        value = payload.get("senseValue")
        return value if value is not None else payload.get("probeValue")


class HydrosEnumInputSensor(HydrosEntity, SensorEntity):
    def __init__(self, device: HydrosDevice, input_name: str) -> None:
        super().__init__(device, unique_id=f"{device.device_id}-{input_name}", name=input_name)
        self._input_name = input_name

    @property
    def native_value(self) -> str | None:
        state: DeviceState = self.coordinator.data
        return state.input_triple_level_label(self._input_name)


class HydrosRawInputSensor(HydrosEntity, SensorEntity):
    """Fallback for any log-series ``sensor_type`` not otherwise handled
    (e.g. ``event``, ``unknown``) -- exposes the raw per-input payload as a
    diagnostic string rather than silently dropping it, since the state
    document's shape is explicitly not locked by the API contract.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, device: HydrosDevice, input_name: str) -> None:
        super().__init__(device, unique_id=f"{device.device_id}-{input_name}", name=input_name)
        self._input_name = input_name

    @property
    def native_value(self) -> str | None:
        state: DeviceState = self.coordinator.data
        payload = state.inputs.get(self._input_name)
        # HA rejects states longer than 255 characters.
        return str(payload)[:255] if payload is not None else None
