from __future__ import annotations

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import HydrosDevice, HydrosStateCoordinator


class HydrosEntity(CoordinatorEntity[HydrosStateCoordinator]):
    """Base entity for anything driven by a device's state coordinator."""

    _attr_has_entity_name = True

    def __init__(self, device: HydrosDevice, *, unique_id: str, name: str) -> None:
        super().__init__(device.state_coordinator)
        self._device = device
        self._attr_unique_id = unique_id
        self._attr_name = name

    @property
    def device_info(self) -> DeviceInfo:
        return self._device.device_info
