"""Base entity for Wallbox BLE."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WallboxBleCoordinator


class WallboxBleEntity(CoordinatorEntity[WallboxBleCoordinator]):
    """Common device/info plumbing for Wallbox BLE entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: WallboxBleCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"wallbox_ble_{coordinator.address}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            name="Wallbox (BLE)",
            manufacturer="Wallbox",
            model="Pulsar",
            connections={("bluetooth", coordinator.address)},
        )
