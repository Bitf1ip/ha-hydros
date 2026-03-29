from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Callable

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .hydros_hub import HydrosHub
from .types import is_binary_output, is_doser_output
from .entity_builders import build_output_binary_description, build_rope_leak_description
from .sensor import (
    OUTPUT_STATE_ALIASES,
    _coerce_int,
    _map_output_state_label,
    _normalize_output_value,
)

_LOGGER = logging.getLogger(__name__)

ROPE_LEAK_SENSE_MODE = "ropeleak"


@dataclass
class HydrosBinarySensorEntityDescription(BinarySensorEntityDescription):
    thing_id: str | None = None
    output_key: str | None = None
    section: str = "Output"
    input_key: str | None = None
    sense_mode: str | None = None


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

    manager = HydrosBinarySensorManager(
        hass=hass,
        hub=hub,
        entry=entry,
        async_add_entities=async_add_entities,
    )
    entry_data["binary_sensor_manager"] = manager
    await manager.async_setup()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    manager = None
    if isinstance(entry_data, dict):
        manager = entry_data.get("binary_sensor_manager")
        entry_data["binary_sensor_manager"] = None

    if manager:
        await manager.async_unload()

    return True


class HydrosBinarySensorManager:
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
        self._entities: dict[str, HydrosBinarySensor] = {}
        self._refresh_lock = asyncio.Lock()
        self._subscribed: set[str] = set()
        self._config_unsubs: list[Callable[[], None]] = []
        self._dosing_unsub: Callable[[], None] | None = None
        self._refresh_unsub: Callable[[], None] | None = None
        self._doser_outputs: set[tuple[str, str]] = set()

    async def async_setup(self) -> None:
        self._setup_config_listeners()
        self._setup_dosing_poll()
        self._setup_periodic_refresh()
        await self._refresh_entities()

    async def async_unload(self) -> None:
        for unsub in self._config_unsubs:
            unsub()
        self._config_unsubs.clear()
        if self._dosing_unsub:
            self._dosing_unsub()
            self._dosing_unsub = None
        if self._refresh_unsub:
            self._refresh_unsub()
            self._refresh_unsub = None
        self._entities.clear()
        self._subscribed.clear()
        self._doser_outputs.clear()

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

    def _handle_config_signal(self, thing_id: str) -> None:
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("Hydros binary config update received for %s", thing_id)

        def _schedule_refresh() -> None:
            self._hass.async_create_task(self._refresh_entities())

        self._hass.loop.call_soon_threadsafe(_schedule_refresh)

    def _setup_dosing_poll(self) -> None:
        if self._dosing_unsub is not None:
            return

        async def _interval_handler(_: datetime) -> None:
            await self._refresh_dosing_logs()

        self._dosing_unsub = async_track_time_interval(
            self._hass,
            _interval_handler,
            timedelta(minutes=5),
        )

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

    async def _refresh_dosing_logs(self) -> None:
        if not self._doser_outputs:
            return

        updated_things: set[str] = set()
        for thing_id, output_key in list(self._doser_outputs):
            await self._hub.async_refresh_dosing_logs(
                thing_id,
                output_key,
            )
            updated_things.add(thing_id)

        for thing_id in updated_things:
            async_dispatcher_send(
                self._hass,
                self._hub.signal_for_collective(thing_id),
                thing_id,
            )

    async def _refresh_entities(self, *_: Any) -> None:
        async with self._refresh_lock:
            description_map = await self._collect_descriptions()
            new_entities: list[HydrosBinarySensor] = []
            current_keys = set(description_map.keys())

            for unique_id, (description, device_info) in description_map.items():
                existing = self._entities.get(unique_id)
                if existing:
                    if existing.refresh_from_description(description, device_info):
                        existing.async_schedule_update_ha_state()
                    continue

                entity = HydrosBinarySensor(
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

            if self._doser_outputs:
                await self._refresh_dosing_logs()

    async def _collect_descriptions(self) -> dict[str, tuple[HydrosBinarySensorEntityDescription, DeviceInfo]]:
        descriptions: dict[str, tuple[HydrosBinarySensorEntityDescription, DeviceInfo]] = {}
        self._doser_outputs.clear()

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

            outputs = config.get("Output")
            if isinstance(outputs, dict):
                for output_key, output_meta in outputs.items():
                    if not is_binary_output(output_meta):
                        continue

                    if is_doser_output(output_meta):
                        self._doser_outputs.add((thing_id, output_key))

                    description = build_output_binary_description(
                        HydrosBinarySensorEntityDescription,
                        entry=self._entry,
                        thing_id=thing_id,
                        output_key=output_key,
                        output_meta=output_meta,
                        device_name=device_name,
                    )
                    descriptions[description.key] = (
                        description,
                        DeviceInfo(
                            identifiers={(DOMAIN, thing_id)},
                            name=device_name,
                            manufacturer=manufacturer,
                            model=model,
                        ),
                    )

            inputs = config.get("Input")
            if isinstance(inputs, dict):
                for input_key, input_meta in inputs.items():
                    if not isinstance(input_meta, dict):
                        continue
                    sense_mode = str(input_meta.get("senseMode") or "").strip().lower()
                    if sense_mode != ROPE_LEAK_SENSE_MODE:
                        continue
                    description = build_rope_leak_description(
                        HydrosBinarySensorEntityDescription,
                        entry=self._entry,
                        thing_id=thing_id,
                        input_key=input_key,
                        input_meta=input_meta,
                        device_name=device_name,
                    )
                    descriptions[description.key] = (
                        description,
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

    async def _remove_entity(self, entity: "HydrosBinarySensor") -> None:
        entity_id = entity.entity_id
        registry = er.async_get(self._hass)
        if entity_id:
            registry_entry = registry.async_get(entity_id)
            if registry_entry:
                registry.async_remove(entity_id)
        if entity.hass is not None and entity.platform is not None:
            await entity.async_remove()


class HydrosBinarySensor(BinarySensorEntity):
    _attr_should_poll = False

    def __init__(
        self,
        *,
        hub: HydrosHub,
        description: HydrosBinarySensorEntityDescription,
        device_info: DeviceInfo,
    ) -> None:
        self._hub = hub
        self.entity_description = description
        self._device_info = device_info
        self._attr_unique_id = description.key
        self._thing_id = description.thing_id or ""
        self._output_key = description.output_key or ""
        self._input_key = description.input_key or ""
        self._section = description.section
        self._sense_mode = description.sense_mode
        self._remove_dispatcher: Callable[[], None] | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return self._device_info

    @property
    def available(self) -> bool:
        if not self._thing_id:
            return False
        last_ts = self._hub.get_latest_status_ts(self._thing_id)
        if not last_ts:
            return False
        delta = (datetime.now(timezone.utc) - last_ts).total_seconds()
        return delta <= 30

    @property
    def is_on(self) -> bool | None:
        if not self._thing_id:
            return None
        if self._section == "Input":
            if not self._input_key:
                return None
            payload = self._hub.get_input_payload(self._thing_id, self._input_key) or {}
            value = None
            if isinstance(payload, dict):
                for key in ("value", "senseValue", "reading", "current", "rawValue"):
                    if key in payload:
                        value = payload.get(key)
                        break
            if value is None:
                value = self._hub.get_input_value(self._thing_id, self._input_key)
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                return None
            return numeric > 0

        if not self._output_key:
            return None
        payload = self._hub.get_output_payload(self._thing_id, self._output_key) or {}
        metadata = self._hub.get_output_metadata(self._thing_id, self._output_key) or {}

        value_state = payload.get("valueState")
        numeric_state = _coerce_int(value_state)
        if numeric_state is None and isinstance(value_state, str):
            alias = OUTPUT_STATE_ALIASES.get(value_state.strip().lower())
            if alias is not None:
                numeric_state = alias

        if numeric_state is not None:
            if numeric_state <= 0:
                return False
            if numeric_state > 1:
                return True

        label = _map_output_state_label(payload, metadata)
        if label == "on":
            return True
        if label == "off":
            return False

        if numeric_state == 1:
            return True
        if numeric_state == -1:
            return False

        return bool(numeric_state)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        payload = self._hub.get_output_payload(self._thing_id, self._output_key) or {}
        metadata = self._hub.get_output_metadata(self._thing_id, self._output_key) or {}

        attrs: dict[str, Any] = {}
        if payload:
            for key, value in payload.items():
                attr_key = f"payload_{key}"
                attrs[attr_key] = value
                normalized_key: str | None = None
                if key in {"voltageI", "current", "powerI", "frequency"}:
                    normalized = _normalize_output_value(key, value)
                    if isinstance(normalized, (int, float)):
                        normalized_key = f"{attr_key}_float"
                        if key == "frequency":
                            normalized = round(normalized, 3)
                        attrs[normalized_key] = normalized
        if metadata:
            for key, value in metadata.items():
                if key == "flowRate":
                    try:
                        value = float(value) / 10.0
                    except (TypeError, ValueError):
                        value = metadata[key]
                attrs[f"meta_{key}"] = value

        label = _map_output_state_label(payload, metadata)
        if label:
            attrs["state_label"] = label

        if is_doser_output(metadata):
            updated = self._hub.get_dosing_total_updated(
                self._thing_id,
                self._output_key,
            )
            if updated:
                attrs["dosing_total_updated"] = updated.isoformat()

        if attrs:
            last_ts = self._hub.get_latest_status_ts(self._thing_id)
            if last_ts:
                attrs["last_update"] = last_ts.isoformat()

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

    def refresh_from_description(
        self,
        description: HydrosBinarySensorEntityDescription,
        device_info: DeviceInfo,
    ) -> bool:
        changed = False
        if self.entity_description != description:
            changed = True
        if self._device_info != device_info:
            changed = True

        self.entity_description = description
        self._device_info = device_info
        self._thing_id = description.thing_id or ""
        self._output_key = description.output_key or ""
        self._input_key = description.input_key or ""
        self._section = description.section
        self._sense_mode = description.sense_mode
        return changed

    def _handle_signal(self, _: str) -> None:
        self.schedule_update_ha_state()
