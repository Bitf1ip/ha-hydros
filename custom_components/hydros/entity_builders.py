from __future__ import annotations

from typing import Any, Callable, Optional, Type

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntityDescription,
)
from homeassistant.components.sensor import SensorDeviceClass, SensorEntityDescription, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower, UnitOfTemperature, UnitOfVolume, UnitOfVolumeFlowRate
from homeassistant.util import slugify

from .types import coerce_int as _coerce_int


def build_input_sensor_description(
    description_cls: Type[SensorEntityDescription],
    *,
    entry: ConfigEntry,
    thing_id: str,
    input_key: str,
    sensor_meta: dict[str, Any],
    device_name: str,
    sense_mode_map: dict[str, dict[str, Any]],
    probe_mode_meta: dict[int, dict[str, Any]],
    round_probe_value: Callable[[float], float],
    map_triple_level: Callable[[float | int], str],
) -> Optional[SensorEntityDescription]:
    sense_mode = str(sensor_meta.get("senseMode") or "").lower()
    sensor_type = str(sensor_meta.get("type") or "").lower()

    if sense_mode == "ropeleak":
        return None

    mapping: dict[str, Any] = dict(sense_mode_map.get(sense_mode, {}))
    suggested_precision: int | None = None
    if sensor_type == "probe":
        probe_mode_value = _coerce_int(sensor_meta.get("probeMode"))
        mapping = dict(probe_mode_meta.get(probe_mode_value, {}))
        suggested_precision = 2

    unit = mapping.get("unit")
    transform: Callable[[float], float] | None = None
    if unit is None and sense_mode == "temp":
        unit = UnitOfTemperature.CELSIUS
    elif unit is None and sense_mode == "flowrate":
        unit = UnitOfVolumeFlowRate.LITERS_PER_HOUR
        mapping["device_class"] = SensorDeviceClass.VOLUME_FLOW_RATE
    elif sensor_type == "probe":
        transform = round_probe_value
    elif sense_mode == "triplelevel":
        transform = map_triple_level

    name = sensor_meta.get("friendlyName") or sensor_meta.get("label") or input_key
    full_name = f"{device_name} {name}" if device_name not in name else name
    slug = slugify(f"{thing_id}-{input_key}")

    return description_cls(
        key=f"{entry.entry_id}-{thing_id}-{slug}",
        name=full_name,
        native_unit_of_measurement=unit,
        device_class=mapping.get("device_class"),
        state_class=mapping.get("state_class"),
        thing_id=thing_id,
        input_key=input_key,
        section="Input",
        value_transform=transform,
        suggested_display_precision=suggested_precision,
    )


def build_output_sensor_description(
    description_cls: Type[SensorEntityDescription],
    *,
    entry: ConfigEntry,
    thing_id: str,
    output_key: str,
    output_meta: dict[str, Any],
    device_name: str,
    output_payload_keys: tuple[str, ...],
    output_value_transforms: dict[str, tuple[str, float]],
) -> Optional[SensorEntityDescription]:
    name = output_meta.get("friendlyName") or output_meta.get("name") or output_key
    full_name = f"{device_name} {name}" if device_name not in name else name
    slug = slugify(f"{thing_id}-output-{output_key}")

    primary_key = None
    for candidate in ("valueState", "powerI", "current", "voltageI", "frequency"):
        if candidate in output_meta or candidate in output_payload_keys:
            primary_key = candidate
            break

    transform = output_value_transforms.get(primary_key or "")
    unit = transform[0] if transform else None
    state_class = SensorStateClass.MEASUREMENT if unit else None

    return description_cls(
        key=f"{entry.entry_id}-{thing_id}-{slug}",
        name=full_name,
        native_unit_of_measurement=unit,
        device_class=None,
        state_class=state_class,
        thing_id=thing_id,
        input_key=output_key,
        section="Output",
        primary_key=primary_key,
    )


def build_output_power_description(
    description_cls: Type[SensorEntityDescription],
    *,
    entry: ConfigEntry,
    thing_id: str,
    output_key: str,
    output_meta: dict[str, Any],
    device_name: str,
) -> Optional[SensorEntityDescription]:
    if not any(key in output_meta for key in ("minPower", "maxPower", "powerAlertLevel", "powerI")):
        return None
    name = output_meta.get("friendlyName") or output_meta.get("name") or output_key
    full_name = f"{device_name} {name}" if device_name not in name else name
    slug = slugify(f"{thing_id}-power-{output_key}")

    return description_cls(
        key=f"{entry.entry_id}-{thing_id}-{slug}",
        name=f"{full_name} Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        thing_id=thing_id,
        input_key=output_key,
        section="Output",
        primary_key="powerI",
    )


def build_doser_today_description(
    description_cls: Type[SensorEntityDescription],
    *,
    entry: ConfigEntry,
    thing_id: str,
    output_key: str,
    output_meta: dict[str, Any],
    device_name: str,
) -> SensorEntityDescription:
    name = output_meta.get("friendlyName") or output_meta.get("name") or output_key
    full_name = f"{device_name} {name}" if device_name not in name else name
    slug = slugify(f"{thing_id}-dosed-today-{output_key}")

    return description_cls(
        key=f"{entry.entry_id}-{thing_id}-{slug}",
        name=f"{full_name} Dosed Today",
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        device_class=None,
        state_class=SensorStateClass.TOTAL,
        thing_id=thing_id,
        input_key=output_key,
        section="DosedToday",
        primary_key=None,
    )


def build_doser_reservoir_description(
    description_cls: Type[SensorEntityDescription],
    *,
    entry: ConfigEntry,
    thing_id: str,
    output_key: str,
    output_meta: dict[str, Any],
    device_name: str,
) -> SensorEntityDescription:
    name = output_meta.get("friendlyName") or output_meta.get("name") or output_key
    full_name = f"{device_name} {name}" if device_name not in name else name
    slug = slugify(f"{thing_id}-reservoir-{output_key}")

    return description_cls(
        key=f"{entry.entry_id}-{thing_id}-{slug}",
        name=f"{full_name} Reservoir Remaining",
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        thing_id=thing_id,
        input_key=output_key,
        section="Output",
        primary_key="reservoir",
    )


def build_collective_description(
    description_cls: Type[SensorEntityDescription],
    *,
    entry: ConfigEntry,
    thing_id: str,
    device_name: str,
) -> SensorEntityDescription:
    slug = slugify(f"{thing_id}-collective-health")
    return description_cls(
        key=f"{entry.entry_id}-{thing_id}-{slug}",
        name=f"{device_name} MQTT Health",
        native_unit_of_measurement=None,
        device_class=None,
        state_class=None,
        thing_id=thing_id,
        input_key=thing_id,
        section="Collective",
        primary_key=None,
        value_transform=None,
    )


def build_collective_mode_description(
    description_cls: Type[SensorEntityDescription],
    *,
    entry: ConfigEntry,
    thing_id: str,
    device_name: str,
) -> SensorEntityDescription:
    slug = slugify(f"{thing_id}-collective-mode")
    return description_cls(
        key=f"{entry.entry_id}-{thing_id}-{slug}",
        name=f"{device_name} Mode",
        native_unit_of_measurement=None,
        device_class=None,
        state_class=None,
        thing_id=thing_id,
        input_key=f"{thing_id}-mode",
        section="CollectiveMode",
        primary_key="mode",
        value_transform=None,
    )


def build_collective_alerts_description(
    description_cls: Type[SensorEntityDescription],
    *,
    entry: ConfigEntry,
    thing_id: str,
    device_name: str,
) -> SensorEntityDescription:
    slug = slugify(f"{thing_id}-alerts")
    return description_cls(
        key=f"{entry.entry_id}-{thing_id}-{slug}",
        name=f"{device_name} Alerts",
        native_unit_of_measurement=None,
        device_class=None,
        state_class=None,
        thing_id=thing_id,
        input_key=thing_id,
        section="CollectiveAlerts",
        primary_key=None,
        value_transform=None,
    )


def build_collective_debug_description(
    description_cls: Type[SensorEntityDescription],
    *,
    entry: ConfigEntry,
    thing_id: str,
    device_name: str,
) -> SensorEntityDescription:
    slug = slugify(f"{thing_id}-debug-sample")
    return description_cls(
        key=f"{entry.entry_id}-{thing_id}-{slug}",
        name=f"{device_name} Debug Sample",
        native_unit_of_measurement=None,
        device_class=None,
        state_class=None,
        thing_id=thing_id,
        input_key=thing_id,
        section="CollectiveDebug",
        primary_key=None,
        value_transform=None,
    )


def build_output_binary_description(
    description_cls: Type[BinarySensorEntityDescription],
    *,
    entry: ConfigEntry,
    thing_id: str,
    output_key: str,
    output_meta: dict[str, Any],
    device_name: str,
) -> BinarySensorEntityDescription:
    name = output_meta.get("friendlyName") or output_meta.get("name") or output_key
    display_name = f"{device_name} {name}" if device_name not in name else name
    slug = slugify(f"{thing_id}-binary-state-{output_key}")

    return description_cls(
        key=f"{entry.entry_id}-{thing_id}-{slug}",
        name=display_name,
        device_class=None,
        thing_id=thing_id,
        output_key=output_key,
        section="Output",
    )


def build_rope_leak_description(
    description_cls: Type[BinarySensorEntityDescription],
    *,
    entry: ConfigEntry,
    thing_id: str,
    input_key: str,
    input_meta: dict[str, Any],
    device_name: str,
) -> BinarySensorEntityDescription:
    name = input_meta.get("friendlyName") or input_meta.get("label") or input_key
    display_name = f"{device_name} {name}" if device_name not in name else name
    slug = slugify(f"{thing_id}-rope-leak-{input_key}")

    return description_cls(
        key=f"{entry.entry_id}-{thing_id}-{slug}",
        name=display_name,
        device_class=BinarySensorDeviceClass.MOISTURE,
        thing_id=thing_id,
        input_key=input_key,
        section="Input",
        sense_mode="ropeleak",
    )
