"""Binary sensor platform for Wallbox BLE.

Read-only state entities:
  - Charging   : whether the charger is actively charging (was a switch; under
                 OCPP you cannot reliably force-start locally, so this reflects
                 state only).
  - OCPP enabled : whether the charger is managed by an OCPP backend. Static;
                   read once at startup.

Note: plug-state ("car connected") detection is not available over BLE on
OCPP-controlled chargers — the coarse BLE status can't distinguish a connected
car while the charger is OCPP-locked, so no "Plugged In" sensor is provided.
"""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import WallboxBleCoordinator
from .entity import WallboxBleEntity

# r_dat.st / r_sta.charger_status code that means actively charging.
STATUS_CHARGING = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Wallbox BLE binary sensors."""
    coordinator: WallboxBleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            WallboxChargingBinarySensor(coordinator),
            WallboxOcppBinarySensor(coordinator),
        ]
    )


def _status_code(data: dict) -> int | None:
    code = (data.get("status") or {}).get("st")
    if code is None:
        code = (data.get("realtime") or {}).get("charger_status")
    try:
        return int(code)
    except (ValueError, TypeError):
        return None


class WallboxChargingBinarySensor(WallboxBleEntity, BinarySensorEntity):
    """On when the charger is actively charging (read-only)."""

    _attr_name = "Charging"
    _attr_icon = "mdi:ev-station"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "charging")

    @property
    def is_on(self) -> bool | None:
        code = _status_code(self.coordinator.data or {})
        return None if code is None else code == STATUS_CHARGING


class WallboxOcppBinarySensor(WallboxBleEntity, BinarySensorEntity):
    """On when the charger is managed by an OCPP backend (static)."""

    _attr_name = "OCPP Enabled"
    _attr_icon = "mdi:cloud-cog"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "ocpp_enabled")

    @property
    def is_on(self) -> bool | None:
        return (self.coordinator.data or {}).get("ocpp_enabled")
