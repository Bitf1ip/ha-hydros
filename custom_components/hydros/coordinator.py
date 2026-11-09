"""Coordinators and per-device runtime bundle for the Hydros integration.

Each physical device gets its own ``HydrosDevice`` bundling the pyhydros2
client, the two coordinators that poll it, and the (mostly static) metadata
needed to build entities. Compared to the old MQTT-based hub, there is no
dispatcher/watchdog/subscription bookkeeping: everything is a plain
``DataUpdateCoordinator`` poll loop, which is the standard Home Assistant
pattern for cloud-polling integrations.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from pyhydros2 import (
    DeviceStatePoller,
    DeviceState,
    HydrosAPIError,
    HydrosAuthError,
    HydrosClient,
    HydrosForbiddenError,
    HydrosRateLimitError,
    LogSeriesInfo,
    OverrideMetadataEntry,
    Receipt,
)

from .const import (
    AWC_EVENT_UPDATE_INTERVAL_SECONDS,
    DOMAIN,
    DOSING_UPDATE_INTERVAL_SECONDS,
    STATE_UPDATE_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

# The cloud snapshot can lag a confirmed write by several poll cycles.
_OPTIMISTIC_HOLD_SECONDS = 60

# The state rate limit is shared by every key for the device, so an occasional
# 429 is expected; ride it out on the last snapshot instead of going unavailable.
_RATE_LIMIT_GRACE_SECONDS = 60


@dataclass
class _Pending:
    patch: dict[str, Any]
    reflected: Callable[[dict[str, Any]], bool]
    deadline: float


class HydrosStateCoordinator(DataUpdateCoordinator[DeviceState]):
    """Poll state and the explicit override document for one device."""

    def __init__(
        self,
        hass: HomeAssistant,
        poller: DeviceStatePoller,
        client: HydrosClient,
        device_name: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"Hydros {device_name} state",
            update_interval=timedelta(seconds=STATE_UPDATE_INTERVAL_SECONDS),
        )
        self._poller = poller
        self._client = client
        self._override_snapshot: dict[str, dict[str, Any]] | None = None
        # Keyed by output name, or None for top-level fields such as ``mode``.
        self._pending: dict[str | None, _Pending] = {}
        self._last_success = 0.0

    async def _async_update_data(self) -> DeviceState:
        try:
            state = await self._poller.async_poll()
        except (HydrosAuthError, HydrosForbiddenError) as err:
            raise ConfigEntryAuthFailed("Hydros rejected the device key") from err
        except HydrosRateLimitError as err:
            if (
                self.data is not None
                and time.monotonic() - self._last_success < _RATE_LIMIT_GRACE_SECONDS
            ):
                _LOGGER.debug("%s: rate limited, keeping the last snapshot", self.name)
                return self.data
            raise UpdateFailed(f"Rate limited by the Hydros API: {err}") from err
        except HydrosAPIError as err:
            raise UpdateFailed(str(err)) from err
        if state is None:
            raise UpdateFailed("Device has no recent state snapshot (offline or never reported)")
        self._last_success = time.monotonic()
        # The state document may omit per-output `override` flags. Read the
        # API's separate override document so scheduled (Auto) outputs and
        # explicit On/Off overrides remain distinguishable.
        try:
            override_state = await self._client.get_overrides()
        except HydrosAPIError as err:
            # Override status is supplemental. Preserve the last successful
            # snapshot and keep ordinary state sensors available on failure.
            _LOGGER.debug("Could not refresh Hydros override state: %s", err)
        else:
            self._override_snapshot = {
                key: {"desired": item.desired, "reported": item.reported}
                for key, item in override_state.overrides.items()
            }

        state = self._apply_pending(state)
        return self._apply_override_snapshot(state)

    def _apply_override_snapshot(self, state: DeviceState) -> DeviceState:
        """Attach explicit override modes keyed by the API's stable output key."""
        if self._override_snapshot is None:
            return state

        raw = dict(state.raw)
        raw["_hydros_override_state"] = dict(self._override_snapshot)
        return DeviceState(raw)

    def _apply_pending(self, state: DeviceState) -> DeviceState:
        now = time.monotonic()
        raw = dict(state.raw)
        outputs = dict(raw.get("Output") or {})
        changed = False
        for target, pending in list(self._pending.items()):
            current = raw if target is None else outputs.get(target, {})
            if now >= pending.deadline or pending.reflected(current):
                del self._pending[target]
                continue
            if target is None:
                raw.update(pending.patch)
            else:
                outputs[target] = {**current, **pending.patch}
            changed = True
        if not changed:
            return state
        raw["Output"] = outputs
        return DeviceState(raw)

    def apply_optimistic(
        self,
        target: str | None,
        patch: dict[str, Any],
        reflected: Callable[[dict[str, Any]], bool],
    ) -> None:
        """Show a device-confirmed change now and keep showing it until a poll
        satisfies ``reflected`` or the hold expires.
        """
        self._pending[target] = _Pending(
            patch, reflected, time.monotonic() + _OPTIMISTIC_HOLD_SECONDS
        )
        if self.data is not None:
            state = self._apply_pending(self.data)
            self.async_set_updated_data(self._apply_override_snapshot(state))

    def apply_optimistic_override(self, key: str, value: Any) -> None:
        """Reflect a device-confirmed override in the override snapshot now.

        The next poll replaces the snapshot with the API's document.
        """
        if self._override_snapshot is None:
            return
        snapshot = dict(self._override_snapshot)
        if value is None:
            snapshot.pop(key, None)
        else:
            snapshot[key] = {"desired": value, "reported": value}
        self._override_snapshot = snapshot


class HydrosDosingCoordinator(DataUpdateCoordinator[dict[str, float]]):
    """Polls today's dosing totals for a device's doser outputs.

    Runs on a slower interval than the state coordinator since it queries
    the log endpoint rather than the lightweight polling-session endpoint.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: HydrosClient,
        doser_names: list[str],
        device_name: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"Hydros {device_name} dosing",
            update_interval=timedelta(seconds=DOSING_UPDATE_INTERVAL_SECONDS),
        )
        self._client = client
        self._doser_names = doser_names

    async def _async_update_data(self) -> dict[str, float]:
        if not self._doser_names:
            return {}
        try:
            return await self._client.get_dosing_totals_today(
                self._doser_names, now=dt_util.now()
            )
        except HydrosAPIError as err:
            raise UpdateFailed(str(err)) from err


@dataclass(frozen=True)
class HydrosAWCEvent:
    """One AWC operation transition reported by the Hydros event log."""

    event_type: str
    message: str
    value: int
    timestamp: str
    timestamp_ms: int
    fill_started_at: str | None = None
    fill_runtime_seconds: float | None = None
    drain_started_at: str | None = None
    drain_runtime_seconds: float | None = None


class HydrosAWCEventCoordinator(DataUpdateCoordinator[list[HydrosAWCEvent]]):
    """Poll the AWC event series and deliver new ATO, Drain, and Fill transitions."""

    _INITIAL_LOOKBACK_MS = 7 * 24 * 60 * 60 * 1000
    _QUERY_OVERLAP_MS = 5 * 60 * 1000
    _SEEN_RETENTION_MS = 24 * 60 * 60 * 1000

    def __init__(self, hass: HomeAssistant, client: HydrosClient, device_name: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"Hydros {device_name} AWC events",
            update_interval=timedelta(seconds=AWC_EVENT_UPDATE_INTERVAL_SECONDS),
        )
        self._client = client
        self._last_query_end_ms: int | None = None
        self._seen: set[tuple[int, str, int]] = set()
        self._active_started_ms: dict[str, int] = {}

    async def _async_update_data(self) -> list[HydrosAWCEvent]:
        end_ms = int(dt_util.utcnow().timestamp() * 1000)
        initial = self._last_query_end_ms is None
        start_ms = (
            max(0, end_ms - self._INITIAL_LOOKBACK_MS)
            if initial
            else max(0, self._last_query_end_ms - self._QUERY_OVERLAP_MS)
        )
        try:
            # AWC is a boolean Output series with message labels, not an event sensor.
            # The API exposes its timestamped state/message points at 10m resolution.
            result = await self._client.query_logs(
                start_ms, end_ms, resolution="10m", names=["AWC"]
            )
        except HydrosAPIError as err:
            raise UpdateFailed(f"Could not read Hydros AWC event history: {err}") from err

        series = result.series.get("AWC")
        parsed = sorted(
            (
                event
                for point in (series.points if series is not None else [])
                if (event := _parse_awc_event_point(point)) is not None
            ),
            key=lambda event: (event.timestamp_ms, event.message, event.value),
        )

        # On startup, seed the de-duplication window without replaying old log
        # events into automations. Later polls deliver each newly-seen record.
        if initial:
            self._seen.update(_awc_event_key(event) for event in parsed)
            self._track_operation_runtime(parsed)
            self._last_query_end_ms = end_ms
            return []

        new_events = [event for event in parsed if _awc_event_key(event) not in self._seen]
        self._seen.update(_awc_event_key(event) for event in new_events)
        cutoff_ms = end_ms - self._SEEN_RETENTION_MS
        self._seen = {key for key in self._seen if key[0] >= cutoff_ms}
        self._last_query_end_ms = end_ms
        return self._track_operation_runtime(new_events)

    def _track_operation_runtime(
        self, events: list[HydrosAWCEvent]
    ) -> list[HydrosAWCEvent]:
        """Pair Drain and Fill stop records with their preceding start times."""
        delivered: list[HydrosAWCEvent] = []
        for event in events:
            if event.message not in {"Drain", "Fill"}:
                delivered.append(event)
                continue

            operation = event.message.lower()
            if event.value == 1:
                self._active_started_ms[operation] = event.timestamp_ms
                delivered.append(event)
                continue

            if event.value == 0:
                start_ms = self._active_started_ms.pop(operation, None)
                if start_ms is not None and start_ms <= event.timestamp_ms:
                    event = replace(
                        event,
                        **{
                            f"{operation}_started_at": _format_log_timestamp(start_ms),
                            f"{operation}_runtime_seconds": (
                                event.timestamp_ms - start_ms
                            )
                            / 1000,
                        },
                    )
            delivered.append(event)
        return delivered


def _parse_awc_event_point(point: tuple[Any, ...]) -> HydrosAWCEvent | None:
    """Parse the documented AWC point shape: [time_ms, value, message]."""
    if len(point) < 3:
        return None

    timestamp_ms = _parse_log_timestamp(point[0])
    message = str(point[2]).strip()
    if timestamp_ms is None or message not in {"ATO", "Drain", "Fill"}:
        return None

    try:
        value = int(point[1])
    except (TypeError, ValueError):
        return None
    if value not in {0, 1}:
        return None

    operation = message.lower()
    action = "started" if value == 1 else "stopped"
    timestamp = _format_log_timestamp(timestamp_ms)
    return HydrosAWCEvent(
        event_type=f"{operation}_{action}",
        message=message,
        value=value,
        timestamp=timestamp,
        timestamp_ms=timestamp_ms,
    )


def _parse_log_timestamp(value: Any) -> int | None:
    """Accept the API's epoch-millisecond timestamps and ISO timestamp form."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        parsed = dt_util.parse_datetime(value)
        if parsed is not None:
            return int(parsed.timestamp() * 1000)
    return None


def _format_log_timestamp(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


def _awc_event_key(event: HydrosAWCEvent) -> tuple[int, str, int]:
    return (event.timestamp_ms, event.message, event.value)


def _raise_if_not_applied(receipt: Receipt | None, *, command: bool) -> None:
    """Raise if a receipted write (``receipt=1``) wasn't confirmed by the
    device itself within the API's ~5 s wait.
    """
    if receipt is None or receipt.applied:
        return
    if receipt.device_code is not None:
        raise HomeAssistantError(
            f"The Hydros device rejected the command (device code {receipt.device_code})"
        )
    if command:
        # Commands to an offline device are discarded, but a slow device may still run one.
        raise HomeAssistantError(
            "The Hydros device did not confirm the command within ~5 s (it may be "
            "offline). It may or may not have run; check the device before retrying."
        )
    raise HomeAssistantError(
        "The Hydros device did not confirm the change within ~5 s (it may be offline). "
        "It is queued and will be applied when the device reconnects."
    )


def is_manual_flow_rate(entry: OverrideMetadataEntry) -> bool:
    """Doser flow-rate sub-channel, which has no state of its own and isn't exposed."""
    return entry.key.endswith(":manualFlowRate")


def override_unique_key(entries: list[OverrideMetadataEntry], key: str, name: str) -> str:
    # The API can return one key for several outputs (e.g. Blue/Green dosing heads).
    if sum(1 for entry in entries if entry.key == key) > 1:
        return f"{key}~{name}"
    return key


@dataclass
class HydrosDevice:
    """Bundles everything a platform needs to build entities for one device."""

    device_id: str
    name: str
    client: HydrosClient
    state_coordinator: HydrosStateCoordinator
    dosing_coordinator: HydrosDosingCoordinator
    awc_event_coordinator: HydrosAWCEventCoordinator
    device_type: str = ""
    override_metadata: list[OverrideMetadataEntry] = field(default_factory=list)
    log_series: list[LogSeriesInfo] = field(default_factory=list)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.device_id)},
            name=self.name,
            manufacturer="CoralVue Hydros",
            model=self.device_type or None,
        )

    def unique_key(self, key: str, name: str) -> str:
        return override_unique_key(self.override_metadata, key, name)

    def override(self, key: str) -> OverrideMetadataEntry | None:
        for entry in self.override_metadata:
            if entry.key == key:
                return entry
        return None

    async def async_put_override(self, key: str, value: Any, name: str) -> None:
        """Set (or clear, with ``value=None``) one override and wait for the
        device to confirm it. The next scheduled poll picks up the result.

        ``name`` is the output's name in the state document.
        """
        try:
            result = await self.client.put_overrides({key: value}, receipt=True)
        except HydrosForbiddenError as err:
            raise HomeAssistantError(
                "This device's API key is read-only; provide a read-write key to control it."
            ) from err
        except HydrosAPIError as err:
            raise HomeAssistantError(str(err)) from err
        _raise_if_not_applied(result.receipt, command=False)
        self.state_coordinator.apply_optimistic_override(key, value)
        self._apply_optimistic_override(name, value)

    def _apply_optimistic_override(self, name: str, value: Any) -> None:
        if value is None:
            patch: dict[str, Any] = {"override": False}
            reflected: Callable[[dict[str, Any]], bool] = lambda out: not out.get("override")
        elif isinstance(value, bool):
            patch = {"override": True, "valueState": 10000 if value else 0}
            reflected = lambda out: bool(out.get("override"))
        else:
            patch = {"override": True, "valueState": value}
            reflected = lambda out: bool(out.get("override")) and out.get("valueState") == value
        self.state_coordinator.apply_optimistic(name, patch, reflected)

    async def async_send_command(self, key: str, command: str, value: int | None = None) -> None:
        """Send a momentary/parameterized override command and wait for the
        device to confirm it. The next scheduled poll picks up the result.
        """
        try:
            result = await self.client.send_override_command(
                key, command, value=value, receipt=True
            )
        except HydrosForbiddenError as err:
            raise HomeAssistantError(
                "This device's API key is read-only; provide a read-write key to control it."
            ) from err
        except HydrosAPIError as err:
            raise HomeAssistantError(str(err)) from err
        _raise_if_not_applied(result.receipt, command=True)
        if key == "mode":
            self.state_coordinator.apply_optimistic(
                None, {"mode": command}, lambda raw: raw.get("mode") == command
            )
