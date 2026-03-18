from __future__ import annotations

import asyncio
import logging
import uuid
import requests
from collections.abc import Callable
from datetime import datetime, timezone, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import (
    CONF_COLLECTIVES,
    CONF_PASSWORD,
    CONF_REGION,
    CONF_USERNAME,
    DEFAULT_REGION,
    DEFAULT_WATCHDOG_INACTIVITY,
    SIGNAL_COLLECTIVE_UPDATED,
    SIGNAL_CONFIG_UPDATED,
)

_LOGGER = logging.getLogger(__name__)

try:
    from pyhydros import HydrosAPI, HydrosAPIError, HydrosAuthError, HydrosMQTTError
except ImportError as err:  # pragma: no cover
    HydrosAPI = None  # type: ignore[assignment]
    HydrosAPIError = Exception  # type: ignore[assignment]
    HydrosAuthError = Exception  # type: ignore[assignment]
    HydrosMQTTError = Exception  # type: ignore[assignment]
    _IMPORT_ERROR = err
else:
    _IMPORT_ERROR = None


class HydrosHub:
    """Coordinate Hydros data access for Home Assistant."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._api: HydrosAPI | None = None
        self._collective_cache: dict[str, dict[str, Any]] = {}
        self._collective_configs: dict[str, dict[str, Any]] = {}
        self._collective_status: dict[str, dict[str, Any]] = {}
        self._config_locks: dict[str, asyncio.Lock] = {}
        self._metadata_lock = asyncio.Lock()
        self._dosing_log_lock = asyncio.Lock()
        self._dosing_total_cache: dict[tuple[str, str], float] = {}
        self._dosing_total_updated: dict[tuple[str, str], datetime] = {}
        self._dosing_day_cache: dict[tuple[str, str], datetime.date] = {}
        self._dosing_seen: dict[tuple[str, str], set[str]] = {}
        self._subscriptions: set[str] = set()
        self._subscription_watchdogs: dict[str, asyncio.TimerHandle] = {}
        self._status_handlers: dict[str, Callable[[str, Any], None]] = {}
        self._debug_samples: dict[str, dict[str, Any]] = {}
        self._mqtt_primary: str | None = None
        self._mqtt_lock = asyncio.Lock()
        self._mqtt_client_id: str = f"ha-hydros-{uuid.uuid4().hex}"
        self._username: str = entry.data[CONF_USERNAME]
        self._password: str = entry.data[CONF_PASSWORD]
        self._region: str = entry.data.get(CONF_REGION, DEFAULT_REGION)
        self.collective_ids: list[str] = list(entry.data.get(CONF_COLLECTIVES, []))

    @property
    def entry_id(self) -> str:
        return self._entry.entry_id

    async def async_setup(self) -> None:
        if _IMPORT_ERROR is not None:
            raise ConfigEntryNotReady from _IMPORT_ERROR

        try:
            await self._hass.async_add_executor_job(self._ensure_client)
            await self.async_refresh_collective_metadata()
        except (HydrosAuthError, HydrosAPIError) as err:
            _LOGGER.error("Hydros authentication or API error: %s", err)
            raise ConfigEntryNotReady from err
        except Exception as err:  # pragma: no cover
            _LOGGER.exception("Unexpected Hydros setup error")
            raise ConfigEntryNotReady from err

    async def async_unload(self) -> None:
        if self._api and getattr(self._api, "mqtt_client", None):
            await self._hass.async_add_executor_job(self._disconnect_mqtt)
        self._api = None
        self._collective_cache.clear()
        self._collective_configs.clear()
        self._config_locks.clear()
        self._collective_status.clear()
        self._subscriptions.clear()
        for handle in self._subscription_watchdogs.values():
            handle.cancel()
        self._subscription_watchdogs.clear()
        self._status_handlers.clear()
        self._mqtt_primary = None

    def _disconnect_mqtt(self) -> None:
        client = getattr(self._api, "mqtt_client", None)
        if client:
            try:
                client.disconnect()
            except Exception:  # pragma: no cover
                _LOGGER.debug("Ignoring error while disconnecting MQTT", exc_info=True)

    def _ensure_client(self) -> HydrosAPI:
        if self._api is None:
            self._api = HydrosAPI(
                username=self._username,
                password=self._password,
                region=self._region,
            )
            self._api.authenticate()
        return self._api

    async def async_call_in_executor(self, func: Callable[..., Any], *args: Any) -> Any:
        return await self._hass.async_add_executor_job(func, *args)

    async def async_refresh_collective_metadata(self) -> None:
        async with self._metadata_lock:
            api = await self._hass.async_add_executor_job(self._ensure_client)
            cache: dict[str, dict[str, Any]] = {}
            for thing_id in self.collective_ids:
                try:
                    metadata = await self._hass.async_add_executor_job(api.get_thing, thing_id)
                except HydrosAPIError as err:
                    _LOGGER.warning("Failed to refresh Hydros thing %s: %s", thing_id, err)
                    continue
                cache[thing_id] = metadata
                initial_status = None
                if isinstance(metadata, dict):
                    initial_status = metadata.get("status") or metadata.get("lastStatus")
                if isinstance(initial_status, dict):
                    merged = self._merge_payloads(
                        self._collective_status.get(thing_id, {}).get("payload", {}),
                        initial_status,
                    )
                    self._collective_status[thing_id] = {
                        "payload": merged,
                        "received": datetime.now(timezone.utc),
                        "message_count": self._collective_status.get(thing_id, {}).get("message_count", 0),
                    }
                    async_dispatcher_send(
                        self._hass, self.signal_for_collective(thing_id), thing_id
                    )
            self._collective_cache = cache

    async def async_collect_debug_sample(self, thing_id: str) -> None:
        if not thing_id:
            return

        api = await self._hass.async_add_executor_job(self._ensure_client)
        config_sample: dict[str, Any] | None = None
        mqtt_sample: dict[str, Any] | None = None

        try:
            config_sample = await self._hass.async_add_executor_job(
                api.download_hydros_data_json, thing_id
            )
        except HydrosAPIError as err:
            _LOGGER.warning("Failed to collect Hydros S3 config for %s: %s", thing_id, err)
        except Exception as err:  # pragma: no cover
            _LOGGER.warning("Unexpected error collecting Hydros S3 config for %s: %s", thing_id, err)

        mqtt_sample = self.get_collective_status_payload(thing_id)
        if mqtt_sample is None:
            _LOGGER.warning("No MQTT payload available yet for %s", thing_id)

        self._debug_samples[thing_id] = {
            "collected": datetime.now(timezone.utc).isoformat(),
            "config": config_sample,
            "mqtt": mqtt_sample,
        }

        _LOGGER.info(
            "Hydros debug sample for %s collected (config=%s, mqtt=%s)",
            thing_id,
            "ok" if config_sample is not None else "missing",
            "ok" if mqtt_sample is not None else "missing",
        )

    def get_collective_metadata(self, thing_id: str) -> dict[str, Any] | None:
        return self._collective_cache.get(thing_id)

    @property
    def api(self) -> HydrosAPI | None:
        return self._api

    async def async_refresh_dosing_logs(
        self,
        thing_id: str,
        output_name: str,
        *,
        count: int = 200,
    ) -> None:
        if not thing_id or not output_name:
            return

        async with self._dosing_log_lock:
            api = await self._hass.async_add_executor_job(self._ensure_client)
            local_now = dt_util.now()
            start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            end_local = start_local + timedelta(days=1)
            start_time = dt_util.as_utc(start_local)
            end_time = dt_util.as_utc(end_local)

            def _fetch_logs() -> list[Any]:
                return api.get_dosing_logs(
                    thing_id,
                    output_name,
                    count=count,
                    skip=0,
                    start=start_time,
                    end=end_time,
                )

            try:
                entries = await self._hass.async_add_executor_job(_fetch_logs)
            except requests.exceptions.HTTPError as err:
                status = getattr(err.response, "status_code", None)
                if status in (401, 403):
                    _LOGGER.warning(
                        "Hydros dosing logs unauthorized for %s/%s; re-authenticating",
                        thing_id,
                        output_name,
                    )
                    try:
                        await self._hass.async_add_executor_job(api.authenticate)
                        entries = await self._hass.async_add_executor_job(_fetch_logs)
                    except Exception as retry_err:
                        _LOGGER.warning(
                            "Failed to refresh dosing logs for %s/%s after re-auth: %s",
                            thing_id,
                            output_name,
                            retry_err,
                        )
                        return
                else:
                    _LOGGER.warning(
                        "Failed to refresh dosing logs for %s/%s: %s",
                        thing_id,
                        output_name,
                        err,
                    )
                    return
            except HydrosAPIError as err:
                _LOGGER.warning(
                    "Failed to refresh dosing logs for %s/%s: %s",
                    thing_id,
                    output_name,
                    err,
                )
                return
            except Exception as err:  # pragma: no cover
                _LOGGER.warning(
                    "Unexpected error refreshing dosing logs for %s/%s: %s",
                    thing_id,
                    output_name,
                    err,
                )
                return

            key = (thing_id, output_name)
            current_day = start_local.date()
            if self._dosing_day_cache.get(key) != current_day:
                self._dosing_day_cache[key] = current_day
                self._dosing_total_cache[key] = 0.0
                self._dosing_seen[key] = set()

            seen = self._dosing_seen.setdefault(key, set())
            total = self._dosing_total_cache.get(key, 0.0)
            changed = False
            for entry in entries or []:
                timestamp = getattr(entry, "timestamp", None)
                quantity = getattr(entry, "quantity_ml", None)
                if quantity is None:
                    continue
                raw = getattr(entry, "raw", None)
                raw_time = None
                if isinstance(raw, dict):
                    raw_time = raw.get("time")
                entry_id = f"{raw_time or timestamp}|{quantity}|{getattr(entry, 'message', None)}"
                if entry_id in seen:
                    continue
                seen.add(entry_id)
                try:
                    total += float(quantity)
                    changed = True
                except (TypeError, ValueError):
                    continue

            if changed:
                self._dosing_total_cache[key] = round(total, 3)
            self._dosing_total_updated[key] = end_time

    def get_dosing_total(self, thing_id: str, output_name: str) -> float | None:
        return self._dosing_total_cache.get((thing_id, output_name))

    def get_dosing_total_updated(self, thing_id: str, output_name: str) -> datetime | None:
        return self._dosing_total_updated.get((thing_id, output_name))

    def get_debug_sample(self, thing_id: str) -> dict[str, Any] | None:
        return self._debug_samples.get(thing_id)

    def signal_for_collective(self, thing_id: str) -> str:
        return SIGNAL_COLLECTIVE_UPDATED.format(
            entry=self._entry.entry_id,
            thing=thing_id,
        )

    def signal_for_config(self, thing_id: str) -> str:
        return SIGNAL_CONFIG_UPDATED.format(
            entry=self._entry.entry_id,
            thing=thing_id,
        )

    def _merge_payloads(self, base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        if not base:
            return dict(incoming)
        merged = dict(base)
        for key, value in incoming.items():
            current = merged.get(key)
            if isinstance(current, dict) and isinstance(value, dict):
                merged_child = self._merge_payloads(current, value)
                if "alert" in current and "alert" not in value:
                    merged_child.pop("alert", None)
                merged[key] = merged_child
            else:
                merged[key] = value
        return merged

    @staticmethod
    def _coerce_inline_config(thing: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(thing, dict):
            return None

        config: dict[str, Any] = {}
        candidate_blocks = [thing]

        embedded = thing.get("config")
        if isinstance(embedded, dict):
            candidate_blocks.append(embedded)

        config_keys = (
            "Input",
            "Output",
            "Option",
            "Schedule",
            "Mode",
            "WiFiOutlet",
            "TpDevice",
            "Device",
            "System",
        )

        for source in candidate_blocks:
            if not isinstance(source, dict):
                continue
            for key in config_keys:
                if key in config:
                    continue
                direct = source.get(key)
                if isinstance(direct, dict):
                    config[key] = direct
                    continue
                alt_key = key.lower()
                alt_value = source.get(alt_key)
                if isinstance(alt_value, dict):
                    config[key] = alt_value

        if "Input" not in config:
            inputs = thing.get("inputs")
            if isinstance(inputs, dict):
                config["Input"] = inputs

        if "Output" not in config:
            outputs = thing.get("outputs")
            if isinstance(outputs, dict):
                config["Output"] = outputs

        return config or None

    async def async_get_collective_config(self, thing_id: str) -> dict[str, Any]:
        if thing_id in self._collective_configs:
            return self._collective_configs[thing_id]

        lock = self._config_locks.setdefault(thing_id, asyncio.Lock())
        async with lock:
            if thing_id in self._collective_configs:
                return self._collective_configs[thing_id]

            api = await self._hass.async_add_executor_job(self._ensure_client)
            config: dict[str, Any] | None = None
            download_error: HydrosAPIError | None = None

            try:
                config = await self._hass.async_add_executor_job(
                    api.download_hydros_data_json, thing_id
                )
                if not isinstance(config, dict):
                    config = {}  # Treat unexpected payloads as empty config
            except HydrosAPIError as err:
                download_error = err
                config = None

            metadata = self._collective_cache.get(thing_id)
            inline_config = self._coerce_inline_config(metadata)
            if inline_config is None:
                try:
                    thing_details = await self._hass.async_add_executor_job(api.get_thing, thing_id)
                except HydrosAPIError:
                    thing_details = None
                inline_config = self._coerce_inline_config(thing_details)

            if inline_config:
                if not config:
                    config = {}
                for key, value in inline_config.items():
                    if key not in config and isinstance(value, dict):
                        config[key] = value

            if not config:
                if download_error is not None:
                    _LOGGER.error("Failed to load Hydros config for %s: %s", thing_id, download_error)
                    raise download_error
                raise HydrosAPIError(f"Hydros config for {thing_id} is empty")

            self._collective_configs[thing_id] = config
            return config

    async def async_subscribe_collective_status(self, thing_id: str) -> None:
        if thing_id in self._subscriptions:
            return

        async with self._mqtt_lock:
            if thing_id in self._subscriptions:
                return

            api = await self._hass.async_add_executor_job(self._ensure_client)
            try:
                await self._hass.async_add_executor_job(
                    self._subscribe_collective_status_blocking, api, thing_id
                )
            except HydrosMQTTError as err:
                _LOGGER.error("Failed to subscribe to Hydros MQTT for %s: %s", thing_id, err)
                raise

            self._subscriptions.add(thing_id)

    def _subscribe_collective_status_blocking(
        self, api: HydrosAPI, thing_id: str
    ) -> None:
        self._ensure_mqtt_connection(api, thing_id)

        def _handler(topic: str, payload: Any) -> None:
            if isinstance(topic, str) and "rsp/PUT/URLconfig/" in topic:
                self._schedule_config_refresh(thing_id)
            self._schedule_status_update(thing_id, payload)

        api.subscribe_thing_status(thing_id, _handler)
        self._hass.loop.call_soon_threadsafe(
            self._register_subscription_handler,
            thing_id,
            _handler,
        )

    def _reregister_collective_status_blocking(
        self, api: HydrosAPI, thing_id: str, handler: Callable[[str, Any], None]
    ) -> None:
        self._ensure_mqtt_connection(api, thing_id)
        api.subscribe_thing_status(thing_id, handler)

    def _force_reconnect_and_register(
        self, api: HydrosAPI, thing_id: str, handler: Callable[[str, Any], None]
    ) -> None:
        self._disconnect_mqtt()
        self._mqtt_primary = None
        self._ensure_mqtt_connection(api, thing_id)
        api.subscribe_thing_status(thing_id, handler)

    def _ensure_mqtt_connection(self, api: HydrosAPI, thing_id: str) -> None:
        if api.mqtt_client and getattr(api.mqtt_client, "connected", False):
            return

        anchor = self._mqtt_primary or thing_id
        api.connect_mqtt(thing_id=anchor, client_id=self._mqtt_client_id)
        self._mqtt_primary = anchor

    def _schedule_status_update(self, thing_id: str, payload: Any) -> None:
        self._hass.loop.call_soon_threadsafe(self._handle_status_update, thing_id, payload)

    def _schedule_config_refresh(self, thing_id: str) -> None:
        self._hass.loop.call_soon_threadsafe(self._handle_config_refresh, thing_id)

    def _register_subscription_handler(self, thing_id: str, handler: Callable[[str, Any], None]) -> None:
        self._status_handlers[thing_id] = handler
        self._ensure_watchdog(thing_id)

    def _ensure_watchdog(self, thing_id: str) -> None:
        handle = self._subscription_watchdogs.pop(thing_id, None)
        if handle:
            handle.cancel()
        if thing_id not in self._subscriptions and thing_id not in self._status_handlers:
            return
        self._subscription_watchdogs[thing_id] = self._hass.loop.call_later(
            DEFAULT_WATCHDOG_INACTIVITY,
            self._subscription_watchdog_fired,
            thing_id,
        )

    def _subscription_watchdog_fired(self, thing_id: str) -> None:
        self._subscription_watchdogs.pop(thing_id, None)
        if thing_id not in self._subscriptions:
            return

        record = self._collective_status.get(thing_id)
        last_received = None if record is None else record.get("received")
        now = datetime.now(timezone.utc)

        if isinstance(last_received, datetime) and now - last_received < timedelta(seconds=DEFAULT_WATCHDOG_INACTIVITY):
            self._ensure_watchdog(thing_id)
            return

        _LOGGER.debug(
            "Hydros collective %s inactive for %ss; retrying subscription",
            thing_id,
            DEFAULT_WATCHDOG_INACTIVITY,
        )
        self._hass.async_create_task(self._async_retry_subscription(thing_id))

    async def _async_retry_subscription(self, thing_id: str) -> None:
        handler = self._status_handlers.get(thing_id)
        if not handler or thing_id not in self._subscriptions:
            return

        api = await self._hass.async_add_executor_job(self._ensure_client)
        try:
            await self._hass.async_add_executor_job(
                self._reregister_collective_status_blocking, api, thing_id, handler
            )
        except HydrosMQTTError as err:
            message = str(err).lower()
            if "mqtt not connected" in message:
                _LOGGER.warning(
                    "Hydros MQTT disconnected for %s; reconnecting",
                    thing_id,
                )
                try:
                    await self._hass.async_add_executor_job(
                        self._force_reconnect_and_register, api, thing_id, handler
                    )
                    return
                except HydrosMQTTError as retry_err:
                    _LOGGER.warning(
                        "Failed to reconnect Hydros MQTT for %s: %s",
                        thing_id,
                        retry_err,
                    )
            else:
                _LOGGER.warning(
                    "Failed to re-register Hydros MQTT for %s: %s",
                    thing_id,
                    err,
                )
        except Exception as err:  # pragma: no cover
            _LOGGER.exception("Unexpected error while retrying Hydros MQTT subscription")
        finally:
            self._ensure_watchdog(thing_id)

    def _handle_status_update(self, thing_id: str, payload: Any) -> None:
        record = self._collective_status.get(thing_id) or {
            "payload": {},
            "received": None,
            "message_count": 0,
        }

        if isinstance(payload, dict):
            record["payload"] = self._merge_payloads(record.get("payload", {}), payload)
        else:
            record.setdefault("payload", {})["_raw"] = payload

        record["received"] = datetime.now(timezone.utc)
        record["message_count"] = int(record.get("message_count", 0)) + 1
        self._collective_status[thing_id] = record

        self._ensure_watchdog(thing_id)
        async_dispatcher_send(self._hass, self.signal_for_collective(thing_id), thing_id)

    def _handle_config_refresh(self, thing_id: str) -> None:
        if thing_id in self._collective_configs:
            self._collective_configs.pop(thing_id, None)
        _LOGGER.debug("Hydros config refresh triggered for %s", thing_id)
        async_dispatcher_send(self._hass, self.signal_for_config(thing_id), thing_id)

    def get_input_metadata(self, thing_id: str, input_key: str) -> dict[str, Any] | None:
        config = self._collective_configs.get(thing_id) or {}
        inputs = config.get("Input")
        if isinstance(inputs, dict):
            data = inputs.get(input_key)
            if isinstance(data, dict):
                return data
        return None

    def get_input_value(self, thing_id: str, input_key: str) -> Any:
        payload = self._collective_status.get(thing_id, {}).get("payload")
        if not isinstance(payload, dict):
            return None

        inputs = payload.get("Input") or payload.get("input")
        if isinstance(inputs, dict):
            sensor_payload = inputs.get(input_key)
            if isinstance(sensor_payload, dict):
                for key in ("value", "reading", "current", "rawValue", "probeValue"):
                    if key in sensor_payload:
                        return sensor_payload.get(key)
                if "senseValue" in sensor_payload:
                    return sensor_payload.get("senseValue")
                if "probeRawValue" in sensor_payload:
                    return sensor_payload.get("probeRawValue")
            return sensor_payload

        return payload.get(input_key)

    def get_input_payload(self, thing_id: str, input_key: str) -> dict[str, Any] | None:
        payload = self._collective_status.get(thing_id, {}).get("payload")
        if not isinstance(payload, dict):
            return None
        inputs = payload.get("Input") or payload.get("input")
        if isinstance(inputs, dict):
            data = inputs.get(input_key)
            if isinstance(data, dict):
                return data
        return None

    def get_output_metadata(self, thing_id: str, output_key: str) -> dict[str, Any] | None:
        config = self._collective_configs.get(thing_id) or {}
        outputs = config.get("Output")
        if isinstance(outputs, dict):
            data = outputs.get(output_key)
            if isinstance(data, dict):
                return data
        return None

    def get_output_payload(self, thing_id: str, output_key: str) -> dict[str, Any] | None:
        payload = self._collective_status.get(thing_id, {}).get("payload")
        if not isinstance(payload, dict):
            return None
        outputs = payload.get("Output") or payload.get("output")
        if isinstance(outputs, dict):
            data = outputs.get(output_key)
            if isinstance(data, dict):
                return data
        return None

    def get_output_value(self, thing_id: str, output_key: str) -> Any:
        payload = self.get_output_payload(thing_id, output_key)
        if not isinstance(payload, dict):
            return payload

        if "valueState" in payload:
            return payload.get("valueState")
        for key in ("powerI", "current", "voltageI", "frequency", "reservoir", "state"):
            if key in payload:
                return payload.get(key)
        return None

    def get_latest_status_ts(self, thing_id: str) -> datetime | None:
        record = self._collective_status.get(thing_id)
        if record:
            return record.get("received")
        return None

    def get_collective_status_payload(self, thing_id: str) -> dict[str, Any] | None:
        record = self._collective_status.get(thing_id)
        payload = None if record is None else record.get("payload")
        if isinstance(payload, dict):
            return payload
        return None

    def get_collective_message_count(self, thing_id: str) -> int:
        record = self._collective_status.get(thing_id)
        if not record:
            return 0
        try:
            return int(record.get("message_count", 0))
        except (TypeError, ValueError):
            return 0

    def is_collective_subscribed(self, thing_id: str) -> bool:
        return thing_id in self._subscriptions
