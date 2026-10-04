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
from dataclasses import dataclass, field
from datetime import timedelta
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

from .const import DOMAIN, DOSING_UPDATE_INTERVAL_SECONDS, STATE_UPDATE_INTERVAL_SECONDS

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
    """Polls the device-state session endpoint on a fixed interval."""

    def __init__(self, hass: HomeAssistant, poller: DeviceStatePoller, device_name: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"Hydros {device_name} state",
            update_interval=timedelta(seconds=STATE_UPDATE_INTERVAL_SECONDS),
        )
        self._poller = poller
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
        return self._apply_pending(state)

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
            self.async_set_updated_data(self._apply_pending(self.data))


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