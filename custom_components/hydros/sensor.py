from __future__ import annotations

import logging
import asyncio
import json
from datetime import timedelta
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfConductivity,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfVolume,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .hydros_hub import HydrosHub
from .types import is_binary_output, is_doser_output, coerce_int as _coerce_int
from .entity_builders import (
    build_collective_alerts_description,
    build_collective_debug_description,
    build_collective_description,
    build_collective_mode_description,
    build_doser_reservoir_description,
    build_doser_today_description,
    build_input_sensor_description,
    build_output_power_description,
    build_output_sensor_description,
)


_LOGGER = logging.getLogger(__name__)


@dataclass
class HydrosSensorEntityDescription(SensorEntityDescription):
    thing_id: str | None = None
    input_key: str | None = None
    section: str = "Input"
    primary_key: str | None = None
    value_transform: Callable[[float], float] | None = None


ALERT_LEVEL_LABELS = {
    0: "None",
    1: "Yellow",
    4: "Orange",
    8: "Red",
}

PROBE_MODE_LABELS = {
    0: "Unused",
    1: "PH",
    2: "ORP (mV)",
    3: "Alk (dKH)",
}

TRIPLE_LEVEL_LABELS = {
    0: "Dry",
    1: "Wet",
    2: "Overflow",
}

_OUTPUT_STATE_LABELS = {
    0: "off",
    1: "on",
    -1: "auto",
}

OUTPUT_STATE_ALIASES = {
    "off": 0,
    "on": 1,
    "auto": -1,
}

_PROBE_MODE_META = {
    1: {
        "unit": "pH",
        "device_class": SensorDeviceClass.PH,
        "state_class": SensorStateClass.MEASUREMENT,
    },
    2: {
        "unit": UnitOfElectricPotential.MILLIVOLT,
        "device_class": None,
        "state_class": SensorStateClass.MEASUREMENT,
    },
    3: {
        "unit": "dKH",
        "device_class": None,
        "state_class": SensorStateClass.MEASUREMENT,
    },
}

SENSE_MODE_MAP = {
    "temp": {
        "device_class": SensorDeviceClass.TEMPERATURE,
        "state_class": SensorStateClass.MEASUREMENT,
    },
    "ph": {
        "unit": "pH",
        "device_class": SensorDeviceClass.PH,
        "state_class": SensorStateClass.MEASUREMENT,
    },
    "orp": {
        "unit": UnitOfElectricPotential.MILLIVOLT,
        "device_class": None,
        "state_class": SensorStateClass.MEASUREMENT,
    },
    "conductivity": {
        "unit": UnitOfConductivity.MICROSIEMENS_PER_CM,
        "device_class": SensorDeviceClass.CONDUCTIVITY,
        "state_class": SensorStateClass.MEASUREMENT,
    },
    "salinity": {
        "unit": "ppt",
        "device_class": None,
        "state_class": SensorStateClass.MEASUREMENT,
    },
    "tds": {
        "unit": "ppm",
        "device_class": None,
        "state_class": SensorStateClass.MEASUREMENT,
    },
    "flowrate": {
        "device_class": SensorDeviceClass.VOLUME_FLOW_RATE,
        "state_class": SensorStateClass.MEASUREMENT,
    },
}

INPUT_META_KEYS = (
    "label",
    "friendlyName",
    "type",
    "unitId",
    "sensePort",
    "senseMode",
    "invisible",
    "minRange",
    "maxRange",
    "minGraphRange",
    "maxGraphRange",
    "minAlarm",
    "maxAlarm",
    "minAlarmDelay",
    "maxAlarmDelay",
    "alertLevel",
    "probeMode",
    "flowRate",
    "offset",
)

INPUT_PAYLOAD_KEYS = (
    "value",
    "reading",
    "current",
    "rawValue",
    "probeValue",
    "probeRawValue",
    "senseValue",
)

OUTPUT_META_KEYS = (
    "name",
    "friendlyName",
    "family",
    "unitId",
    "type",
    "onTemp",
    "offTemp",
    "input",
    "input2",
    "outputDevice",
    "fallback",
    "showAdvanced",
    "excludedModes",
    "dependency",
    "invisible",
    "minPower",
    "maxPower",
    "powerAlertLevel",
)

OUTPUT_PAYLOAD_KEYS = (
    "valueState",
    "state",
    "powerI",
    "current",
    "voltageI",
    "frequency",
    "reservoir",
)

OUTPUT_VALUE_TRANSFORMS = {
    "powerI": (UnitOfPower.WATT, 0.1),
    "current": (UnitOfElectricCurrent.AMPERE, 0.001),
    "voltageI": (UnitOfElectricPotential.VOLT, 0.1),
    "frequency": (UnitOfFrequency.HERTZ, 1.0),
    "reservoir": (UnitOfVolume.MILLILITERS, 1.0),
}

COLLECTIVE_STATUS_FIELDS = (
    "collectiveStatus",
    "collectiveMaster",
    "mode",
    "millis",
    "hostname",
    "time",
    "bootTime",
    "temperatureI",
    "version",
    "build",
)

COLLECTIVE_HEARTBEAT_OFFLINE_SECONDS = 30
COLLECTIVE_HEARTBEAT_STALE_SECONDS = 300


def _normalize_output_value(key: str | None, value: Any) -> Any:
    transform = OUTPUT_VALUE_TRANSFORMS.get(key or "")
    if not transform:
        return value

    _, scale = transform

    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return value

    normalized = numeric * scale
    return round(normalized, 3)




def _round_probe_value(value: float) -> float:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return value


def _map_triple_level(value: float | int) -> str:
    try:
        index = int(round(float(value)))
    except (TypeError, ValueError):
        return str(value)
    return TRIPLE_LEVEL_LABELS.get(index, str(index))


def _map_output_state_label(payload: dict[str, Any], metadata: dict[str, Any] | None) -> str | None:
    if not isinstance(payload, dict):
        return None

    raw_state = payload.get("valueState")
    if raw_state is None:
        raw_state = payload.get("state")
    if raw_state is None and isinstance(metadata, dict):
        raw_state = metadata.get("state")

    if raw_state is None:
        return None

    if isinstance(raw_state, str):
        normalized = raw_state.strip().lower()
        if normalized in OUTPUT_STATE_ALIASES:
            state_value = OUTPUT_STATE_ALIASES[normalized]
        else:
            return raw_state
    else:
        try:
            state_value = int(raw_state)
        except (TypeError, ValueError):
            return str(raw_state)

    return _OUTPUT_STATE_LABELS.get(state_value, str(state_value))


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    if isinstance(entry_data, HydrosHub):
        entry_data = {"hub": entry_data}
        hass.data[DOMAIN][entry.entry_id] = entry_data

    hub: HydrosHub = entry_data["hub"]

    manager = HydrosSensorManager(
        hass=hass,
        hub=hub,
        entry=entry,
        async_add_entities=async_add_entities,
    )
    entry_data["sensor_manager"] = manager
    await manager.async_setup()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    manager = None
    if isinstance(entry_data, dict):
        manager = entry_data.get("sensor_manager")
        entry_data["sensor_manager"] = None

    if manager:
        await manager.async_unload()

    return True


class HydrosSensorManager:
    def __init__(
        self,
        *,
        hass: HomeAssistant,
        hub: HydrosHub,
        entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback,
    ) -> None:
        self._hass = hass
        self._hub = hub
        self._entry = entry
        self._async_add_entities = async_add_entities
        self._entities: dict[str, HydrosSensor] = {}
        self._refresh_lock = asyncio.Lock()
        self._subscribed: set[str] = set()
        self._config_unsubs: list[Callable[[], None]] = []
        self._refresh_unsub: Callable[[], None] | None = None

    async def async_setup(self) -> None:
        self._setup_config_listeners()
        self._setup_periodic_refresh()
        await self._refresh_entities()

    async def async_unload(self) -> None:
        for unsub in self._config_unsubs:
            unsub()
        self._config_unsubs.clear()
        if self._refresh_unsub:
            self._refresh_unsub()
            self._refresh_unsub = None
        self._entities.clear()
        self._subscribed.clear()

    async def _refresh_entities(self, *_: Any) -> None:
        async with self._refresh_lock:
            description_map = await self._collect_descriptions()
            new_entities: list[HydrosSensor] = []
            current_keys = set(description_map.keys())
            changed_count = 0

            self._remove_stale_power_entities(current_keys)

            for unique_id, (description, device_info) in description_map.items():
                existing = self._entities.get(unique_id)
                if existing:
                    if existing.refresh_from_description(description, device_info):
                        existing.async_schedule_update_ha_state()
                        changed_count += 1
                    continue

                entity = HydrosSensor(
                    hub=self._hub,
                    description=description,
                    device_info=device_info,
                )
                self._entities[unique_id] = entity
                new_entities.append(entity)

            removed_keys = [key for key in list(self._entities.keys()) if key not in current_keys]
            for unique_id in removed_keys:
                entity = self._entities.pop(unique_id)
                await self._remove_entity(entity)

            if new_entities:
                self._async_add_entities(new_entities)

            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "Hydros sensor refresh completed: %d new, %d updated, %d removed",
                    len(new_entities),
                    changed_count,
                    len(removed_keys),
                )

    def _remove_stale_power_entities(self, current_keys: set[str]) -> None:
        registry = er.async_get(self._hass)
        for entry in list(registry.entities.values()):
            if entry.config_entry_id != self._entry.entry_id:
                continue
            if entry.platform != DOMAIN:
                continue
            unique_id = entry.unique_id
            if not unique_id or unique_id in current_keys:
                continue
            if "-power-" not in unique_id:
                continue
            _LOGGER.debug("Removing stale Hydros power entity %s", entry.entity_id)
            registry.async_remove(entry.entity_id)

    def _setup_config_listeners(self) -> None:
        if self._config_unsubs:
            return
        for thing_id in self._hub.collective_ids:
            signal = self._hub.signal_for_config(thing_id)
            unsub = async_dispatcher_connect(
                self._hass,
                signal,
                self._handle_config_signal,
            )
            self._config_unsubs.append(unsub)

    def _setup_periodic_refresh(self) -> None:
        if self._refresh_unsub is not None:
            return

        async def _interval_handler(_: datetime) -> None:
            await self._refresh_entities()

        self._refresh_unsub = async_track_time_interval(
            self._hass,
            _interval_handler,
            timedelta(minutes=30),
        )

    def _handle_config_signal(self, thing_id: str) -> None:
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("Hydros config update received for %s", thing_id)
        # Ensure refresh scheduling happens on the Home Assistant event loop thread
        def _schedule_refresh() -> None:
            self._hass.async_create_task(self._refresh_entities())

        self._hass.loop.call_soon_threadsafe(_schedule_refresh)

    async def _collect_descriptions(self) -> dict[str, tuple[HydrosSensorEntityDescription, DeviceInfo]]:
        descriptions: dict[str, tuple[HydrosSensorEntityDescription, DeviceInfo]] = {}

        for thing_id in self._hub.collective_ids:
            try:
                config = await self._hub.async_get_collective_config(thing_id)
            except Exception as err:
                _LOGGER.warning("Hydros failed to load config for %s: %s", thing_id, err)
                continue

            metadata = self._hub.get_collective_metadata(thing_id) or {}
            device_name = metadata.get("friendlyName") or metadata.get("thingName") or thing_id
            manufacturer = metadata.get("manufacturer") or "Hydros"
            model = metadata.get("thingType") or metadata.get("type")

            inputs = config.get("Input")
            if isinstance(inputs, dict):
                for input_key, sensor_meta in inputs.items():
                    if not isinstance(sensor_meta, dict):
                        continue

                    description = build_input_sensor_description(
                        HydrosSensorEntityDescription,
                        entry=self._entry,
                        thing_id=thing_id,
                        input_key=input_key,
                        sensor_meta=sensor_meta,
                        device_name=device_name,
                        sense_mode_map=SENSE_MODE_MAP,
                        probe_mode_meta=_PROBE_MODE_META,
                        round_probe_value=_round_probe_value,
                        map_triple_level=_map_triple_level,
                    )
                    if description is None:
                        continue

                    descriptions[description.key] = (
                        description,
                        DeviceInfo(
                            identifiers={(DOMAIN, thing_id)},
                            name=device_name,
                            manufacturer=manufacturer,
                            model=model,
                        ),
                    )

            outputs = config.get("Output")
            if isinstance(outputs, dict):
                for output_key, output_meta in outputs.items():
                    if not isinstance(output_meta, dict):
                        continue

                    if is_doser_output(output_meta):
                        history_description = build_doser_today_description(
                            HydrosSensorEntityDescription,
                            entry=self._entry,
                            thing_id=thing_id,
                            output_key=output_key,
                            output_meta=output_meta,
                            device_name=device_name,
                        )
                        descriptions[history_description.key] = (
                            history_description,
                            DeviceInfo(
                                identifiers={(DOMAIN, thing_id)},
                                name=device_name,
                                manufacturer=manufacturer,
                                model=model,
                            ),
                        )

                        reservoir_description = build_doser_reservoir_description(
                            HydrosSensorEntityDescription,
                            entry=self._entry,
                            thing_id=thing_id,
                            output_key=output_key,
                            output_meta=output_meta,
                            device_name=device_name,
                        )
                        descriptions[reservoir_description.key] = (
                            reservoir_description,
                            DeviceInfo(
                                identifiers={(DOMAIN, thing_id)},
                                name=device_name,
                                manufacturer=manufacturer,
                                model=model,
                            ),
                        )

                    power_description = build_output_power_description(
                        HydrosSensorEntityDescription,
                        entry=self._entry,
                        thing_id=thing_id,
                        output_key=output_key,
                        output_meta=output_meta,
                        device_name=device_name,
                    )
                    if power_description is not None:
                        descriptions[power_description.key] = (
                            power_description,
                            DeviceInfo(
                                identifiers={(DOMAIN, thing_id)},
                                name=device_name,
                                manufacturer=manufacturer,
                                model=model,
                            ),
                        )

                    if is_binary_output(output_meta):
                        continue

                    description = build_output_sensor_description(
                        HydrosSensorEntityDescription,
                        entry=self._entry,
                        thing_id=thing_id,
                        output_key=output_key,
                        output_meta=output_meta,
                        device_name=device_name,
                        output_payload_keys=OUTPUT_PAYLOAD_KEYS,
                        output_value_transforms=OUTPUT_VALUE_TRANSFORMS,
                    )
                    if description is None:
                        continue

                    descriptions[description.key] = (
                        description,
                        DeviceInfo(
                            identifiers={(DOMAIN, thing_id)},
                            name=device_name,
                            manufacturer=manufacturer,
                            model=model,
                        ),
                    )

            collective_description = build_collective_description(
                HydrosSensorEntityDescription,
                entry=self._entry,
                thing_id=thing_id,
                device_name=device_name,
            )
            descriptions[collective_description.key] = (
                collective_description,
                DeviceInfo(
                    identifiers={(DOMAIN, thing_id)},
                    name=device_name,
                    manufacturer=manufacturer,
                    model=model,
                ),
            )

            mode_description = build_collective_mode_description(
                HydrosSensorEntityDescription,
                entry=self._entry,
                thing_id=thing_id,
                device_name=device_name,
            )
            descriptions[mode_description.key] = (
                mode_description,
                DeviceInfo(
                    identifiers={(DOMAIN, thing_id)},
                    name=device_name,
                    manufacturer=manufacturer,
                    model=model,
                ),
            )

            alerts_description = build_collective_alerts_description(
                HydrosSensorEntityDescription,
                entry=self._entry,
                thing_id=thing_id,
                device_name=device_name,
            )
            descriptions[alerts_description.key] = (
                alerts_description,
                DeviceInfo(
                    identifiers={(DOMAIN, thing_id)},
                    name=device_name,
                    manufacturer=manufacturer,
                    model=model,
                ),
            )

            debug_description = build_collective_debug_description(
                HydrosSensorEntityDescription,
                entry=self._entry,
                thing_id=thing_id,
                device_name=device_name,
            )
            descriptions[debug_description.key] = (
                debug_description,
                DeviceInfo(
                    identifiers={(DOMAIN, thing_id)},
                    name=device_name,
                    manufacturer=manufacturer,
                    model=model,
                ),
            )

            if thing_id not in self._subscribed:
                try:
                    await self._hub.async_subscribe_collective_status(thing_id)
                except Exception as err:
                    _LOGGER.warning("Hydros failed to subscribe to %s: %s", thing_id, err)
                else:
                    self._subscribed.add(thing_id)

        return descriptions

    async def _remove_entity(self, entity: "HydrosSensor") -> None:
        entity_id = entity.entity_id
        registry = er.async_get(self._hass)
        if entity_id:
            registry_entry = registry.async_get(entity_id)
            if registry_entry:
                registry.async_remove(entity_id)
        if entity.hass is not None and entity.platform is not None:
            await entity.async_remove()


class HydrosSensor(SensorEntity):
    _attr_should_poll = False

    def __init__(
        self,
        *,
        hub: HydrosHub,
        description: HydrosSensorEntityDescription,
        device_info: DeviceInfo,
    ) -> None:
        self._hub = hub
        self.entity_description = description
        self._device_info = device_info
        self._attr_unique_id = description.key
        self._thing_id = description.thing_id or ""
        self._input_key = description.input_key or ""
        self._section = description.section
        self._primary_key = description.primary_key
        self._value_transform = description.value_transform
        self._last_health_state: str | None = None
        self._attr_should_poll = description.section == "Collective"
        self._self_dispatch = False
        self._remove_dispatcher: Callable[[], None] | None = None

    def refresh_from_description(
        self, description: HydrosSensorEntityDescription, device_info: DeviceInfo
    ) -> bool:
        changed = False
        if self.entity_description != description:
            changed = True
        if self._device_info != device_info:
            changed = True

        self.entity_description = description
        self._device_info = device_info
        self._thing_id = description.thing_id or ""
        self._input_key = description.input_key or ""
        self._section = description.section
        self._primary_key = description.primary_key
        self._value_transform = description.value_transform
        self._attr_should_poll = description.section == "Collective"
        if self._section == "Collective":
            self._last_health_state = None
        self._self_dispatch = False

        return changed

    @property
    def device_info(self) -> DeviceInfo:
        return self._device_info

    @property
    def available(self) -> bool:
        if not self._thing_id:
            return False
        if self._section == "Collective":
            return True
        return self._collective_is_online()

    def _compute_collective_health_state(self) -> str:
        last_ts = self._hub.get_latest_status_ts(self._thing_id)
        if not last_ts:
            return "unknown"
        delta = (datetime.now(timezone.utc) - last_ts).total_seconds()
        if delta > COLLECTIVE_HEARTBEAT_OFFLINE_SECONDS:
            return "offline"
        return "online"

    @property
    def native_value(self) -> Any:
        if not self._thing_id or not self._input_key:
            return None
        if self._section == "Collective":
            if self._last_health_state is None:
                self._last_health_state = self._compute_collective_health_state()
            return self._last_health_state

        if self._section == "CollectiveAlerts":
            alerts = self._collect_alert_messages()
            if not alerts:
                return ""
            return " | ".join(alerts)

        if self._section == "CollectiveDebug":
            sample = self._hub.get_debug_sample(self._thing_id)
            if not sample:
                return ""
            collected = sample.get("collected")
            if collected:
                return collected
            return ""

        if self._section == "DosedToday":
            total = self._hub.get_dosing_total(self._thing_id, self._input_key)
            if total is None:
                return 0.0
            return total

        if self._section == "CollectiveMode":
            payload = self._hub.get_collective_status_payload(self._thing_id) or {}
            mode = payload.get("mode")
            return mode or "unknown"

        metadata: dict[str, Any] | None = None
        if self._section == "Output":
            metadata = self._hub.get_output_metadata(self._thing_id, self._input_key)
            payload = self._hub.get_output_payload(self._thing_id, self._input_key)
            value = payload if payload is not None else self._hub.get_output_value(
                self._thing_id, self._input_key
            )
        else:
            value = self._hub.get_input_value(self._thing_id, self._input_key)

        def _coerce_numeric(candidate: Any) -> float | int | None:
            if isinstance(candidate, (int, float)):
                return candidate
            if isinstance(candidate, str):
                try:
                    return float(candidate)
                except ValueError:
                    return None
            return None

        if isinstance(value, dict):
            base_key_order = (
                "senseValue",
                "probeValue",
                "value",
                "reading",
                "current",
                "rawValue",
                "probeRawValue",
                "state",
                "valueState",
                "powerI",
                "voltageI",
                "frequency",
            )
            if self._section == "Output":
                ordered_keys = []
                if self._primary_key:
                    ordered_keys.append(self._primary_key)
                ordered_keys.extend(
                    key for key in base_key_order if key not in ordered_keys
                )
            else:
                ordered_keys = base_key_order

            if self._section == "Output" and self._primary_key == "valueState":
                override_state = self._interpret_output_state(value, metadata)
                if override_state is not None:
                    return override_state

            for key in ordered_keys:
                numeric = _coerce_numeric(value.get(key))
                if numeric is not None:
                    if self._section == "Output":
                        return _normalize_output_value(key, numeric)
                    return self._apply_input_transform(numeric)
            return None

        numeric_value = _coerce_numeric(value)
        if numeric_value is not None:
            if self._section == "Output":
                return _normalize_output_value(self._primary_key, numeric_value)
            return self._apply_input_transform(numeric_value)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self._section == "Collective":
            attrs: dict[str, Any] = {}
            last_ts = self._hub.get_latest_status_ts(self._thing_id)
            now = datetime.now(timezone.utc)

            if last_ts:
                attrs["last_message"] = last_ts.isoformat()
                attrs["seconds_since_last_message"] = round((now - last_ts).total_seconds(), 3)
            else:
                attrs["seconds_since_last_message"] = None

            attrs["mqtt_subscribed"] = self._hub.is_collective_subscribed(self._thing_id)
            attrs["message_count"] = self._hub.get_collective_message_count(self._thing_id)
            payload = self._hub.get_collective_status_payload(self._thing_id) or {}
            for key in COLLECTIVE_STATUS_FIELDS:
                if key in payload:
                    attrs[key] = payload[key]

            attrs["heartbeat_offline_after_seconds"] = COLLECTIVE_HEARTBEAT_OFFLINE_SECONDS
            attrs["heartbeat_stale_after_seconds"] = COLLECTIVE_HEARTBEAT_STALE_SECONDS

            return attrs or None

        if self._section == "CollectiveMode":
            payload = self._hub.get_collective_status_payload(self._thing_id) or {}
            attrs = {
                "mode": payload.get("mode"),
                "collectiveStatus": payload.get("collectiveStatus"),
                "collectiveMaster": payload.get("collectiveMaster"),
                "hostname": payload.get("hostname"),
                "version": payload.get("version"),
                "build": payload.get("build"),
                "last_update": None,
            }

            ts = self._hub.get_latest_status_ts(self._thing_id)
            if ts:
                attrs["last_update"] = ts.isoformat()

            return {k: v for k, v in attrs.items() if v is not None} or None

        if self._section == "CollectiveAlerts":
            alerts = self._collect_alert_messages()
            if not alerts:
                return {
                    "alerts": [],
                    "alert_count": 0,
                }
            return {
                "alerts": alerts,
                "alert_count": len(alerts),
            }

        if self._section == "CollectiveDebug":
            sample = self._hub.get_debug_sample(self._thing_id)
            if not sample:
                return {
                    "collected": None,
                    "config_json": None,
                    "mqtt_json": None,
                }
            config = sample.get("config")
            mqtt = sample.get("mqtt")
            attrs = {"collected": sample.get("collected")}
            try:
                attrs["config_json"] = json.dumps(config, sort_keys=True)
            except (TypeError, ValueError):
                attrs["config_json"] = None
            try:
                attrs["mqtt_json"] = json.dumps(mqtt, sort_keys=True)
            except (TypeError, ValueError):
                attrs["mqtt_json"] = None
            return attrs

        if self._section == "DosedToday":
            attrs: dict[str, Any] = {}
            total_updated = self._hub.get_dosing_total_updated(self._thing_id, self._input_key)
            if total_updated:
                attrs["dosing_total_updated"] = total_updated.isoformat()
            return attrs or None

        if self._section == "Output":
            metadata = self._hub.get_output_metadata(self._thing_id, self._input_key) or {}
            payload = self._hub.get_output_payload(self._thing_id, self._input_key) or {}
            meta_keys = OUTPUT_META_KEYS
            payload_keys = OUTPUT_PAYLOAD_KEYS
        else:
            metadata = self._hub.get_input_metadata(self._thing_id, self._input_key) or {}
            payload = self._hub.get_input_payload(self._thing_id, self._input_key) or {}
            meta_keys = INPUT_META_KEYS
            payload_keys = INPUT_PAYLOAD_KEYS

        attrs: dict[str, Any] = {}
        for key in meta_keys:
            if key in metadata:
                value = metadata[key]
                if key == "flowRate":
                    try:
                        value = float(value) / 10.0
                    except (TypeError, ValueError):
                        value = metadata[key]
                attrs[key] = value
        for key in payload_keys:
            if key in payload and f"last_{key}" not in attrs:
                value = payload[key]
                if self._section == "Output" and key in OUTPUT_VALUE_TRANSFORMS:
                    normalized = _normalize_output_value(key, value)
                    if isinstance(normalized, (int, float)):
                        if key == "frequency":
                            normalized = normalized / 100.0
                        elif key == "voltageI":
                            normalized = normalized / 10.0
                    attrs[f"last_{key}"] = normalized
                    unit = OUTPUT_VALUE_TRANSFORMS[key][0]
                    attrs[f"last_{key}_unit"] = unit
                elif self._section != "Output":
                    transformed = self._transform_attribute_value(value)
                    attrs[f"last_{key}"] = transformed
                    if transformed != value:
                        attrs[f"last_{key}_raw"] = value
                else:
                    attrs[f"last_{key}"] = value

        if self._section == "Output":
            label = _map_output_state_label(payload, metadata)
            if label:
                attrs["valueState_label"] = label
            if isinstance(payload, dict) and "valueState" in payload:
                attrs.setdefault("valueState_raw", payload["valueState"])
        else:
            alert_level_value = _coerce_int(metadata.get("alertLevel"))
            if alert_level_value is not None:
                label = ALERT_LEVEL_LABELS.get(alert_level_value)
                if label:
                    attrs["alertLevel_label"] = label
            probe_mode_value = _coerce_int(metadata.get("probeMode"))
            if probe_mode_value is not None:
                label = PROBE_MODE_LABELS.get(probe_mode_value)
                if label:
                    attrs["probeMode_label"] = label

        ts = self._hub.get_latest_status_ts(self._thing_id)
        if ts:
            attrs["last_update"] = ts.isoformat()

        collective = self._hub.get_collective_metadata(self._thing_id)
        if isinstance(collective, dict) and collective.get("serialNum"):
            attrs.setdefault("serial_number", collective.get("serialNum"))

        return attrs or None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        self._remove_dispatcher = async_dispatcher_connect(
            self.hass,
            self._hub.signal_for_collective(self._thing_id),
            self._handle_signal,
        )

        if self._thing_id:
            await self._hub.async_subscribe_collective_status(self._thing_id)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_dispatcher:
            self._remove_dispatcher()
            self._remove_dispatcher = None
        await super().async_will_remove_from_hass()

    async def async_update(self) -> None:
        if self._section != "Collective":
            return

        current = self._compute_collective_health_state()
        previous = self._last_health_state
        self._last_health_state = current

        if current != previous and self._thing_id:
            self._self_dispatch = True
            async_dispatcher_send(
                self.hass,
                self._hub.signal_for_collective(self._thing_id),
                self._thing_id,
            )

    def _handle_signal(self, _: str) -> None:
        # Ensure state updates happen on the Home Assistant event loop thread
        if self._section == "Collective":
            if self._self_dispatch:
                self._self_dispatch = False
            else:
                self._last_health_state = None
        self.schedule_update_ha_state()

    def _apply_input_transform(self, numeric: float | int) -> float | int:
        if not self._value_transform:
            return numeric
        try:
            return self._value_transform(float(numeric))
        except (TypeError, ValueError):
            return numeric

    def _transform_attribute_value(self, value: Any) -> Any:
        if not self._value_transform:
            return value
        try:
            return self._value_transform(float(value))
        except (TypeError, ValueError):
            return value

    def _interpret_output_state(
        self, payload: dict[str, Any], metadata: dict[str, Any] | None
    ) -> str | None:
        return _map_output_state_label(payload, metadata)

    def _collective_is_online(self) -> bool:
        if not self._thing_id:
            return False
        last_ts = self._hub.get_latest_status_ts(self._thing_id)
        if not last_ts:
            return False
        delta = (datetime.now(timezone.utc) - last_ts).total_seconds()
        return delta <= COLLECTIVE_HEARTBEAT_OFFLINE_SECONDS

    def _collect_alert_messages(self) -> list[str]:
        payload = self._hub.get_collective_status_payload(self._thing_id) or {}
        alerts: list[str] = []

        def _collect(section_payload: Any) -> None:
            if not isinstance(section_payload, dict):
                return
            for key, data in section_payload.items():
                if not isinstance(data, dict):
                    continue
                alert = data.get("alert")
                if alert is None:
                    continue
                text = str(alert).strip()
                if not text:
                    continue
                alerts.append(f"{key}: {text}")

        _collect(payload.get("Input") or payload.get("input"))
        _collect(payload.get("Output") or payload.get("output"))

        return alerts
