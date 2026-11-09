"""Hydros activity events backed by the official API's output log."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HydrosAWCEvent, HydrosAWCEventCoordinator, HydrosDevice


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one event entity per Hydros device."""
    devices: dict[str, HydrosDevice] = hass.data[DOMAIN][entry.entry_id]["devices"]
    async_add_entities(HydrosAWCActivityEvent(device) for device in devices.values())


class HydrosAWCActivityEvent(CoordinatorEntity[HydrosAWCEventCoordinator], EventEntity):
    """Expose ATO, Drain, and Fill transitions as Home Assistant events."""

    _attr_has_entity_name = True
    _attr_event_types = [
        "ato_started",
        "ato_stopped",
        "drain_started",
        "drain_stopped",
        "fill_started",
        "fill_stopped",
    ]

    def __init__(self, device: HydrosDevice) -> None:
        super().__init__(device.awc_event_coordinator)
        self._attr_unique_id = f"{device.device_id}-awc-activity"
        self._attr_name = "AWC Activity"
        self._attr_device_info = device.device_info
        self._delivered: list[HydrosAWCEvent] | None = None

    def _handle_coordinator_update(self) -> None:
        """Emit every new log point, including multiple events from one poll."""
        data = self.coordinator.data
        # A failed poll keeps the previous data and still notifies listeners;
        # deliver each successful poll's events only once.
        if not self.coordinator.last_update_success or data is self._delivered:
            super()._handle_coordinator_update()
            return
        self._delivered = data
        for event in data or []:
            self._trigger_event(
                event.event_type,
                {
                    "message": event.message,
                    "value": event.value,
                    "event_timestamp": event.timestamp,
                    "event_timestamp_ms": event.timestamp_ms,
                    "fill_started_at": event.fill_started_at,
                    "fill_runtime_seconds": event.fill_runtime_seconds,
                    "drain_started_at": event.drain_started_at,
                    "drain_runtime_seconds": event.drain_runtime_seconds,
                },
            )
            # Write after each event so rapid Fill start/stop pairs remain
            # distinct state changes and can each trigger an automation.
            self.async_write_ha_state()
        super()._handle_coordinator_update()
