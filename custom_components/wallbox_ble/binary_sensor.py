"""Binary sensor platform for Wallbox BLE.

Read-only state entities:
  - Charging   : whether the charger is actively charging (was a switch; under
                 OCPP you cannot reliably force-start locally, so this reflects
                 state only).
  - Plugged in : whether a car is connected, derived from the charger status
                 code (`r_dat.st`). Available on non-OCPP chargers only; on
                 OCPP-controlled chargers plug state can't be deduced over BLE
                 (status is pinned at Locked), so the sensor reports unavailable
                 there.
  - OCPP enabled : whether the charger is managed by an OCPP backend. Static;
                   read once at startup.

Note: on OCPP-controlled chargers the coarse BLE status can't distinguish a
connected car while the charger is OCPP-locked, so the "Plugged In" sensor is
created but reports unavailable there rather than a misleading plug state.
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

# Status codes (r_dat.st) that mean a car is connected:
#   1 = Charging, 2 = Connected waiting for car, 3 = Connected waiting for
#   schedule, 4 = Paused, 5 = Schedule ended, 8 = Connected waiting for current
#   assignation. Code 6 (Locked, ambiguous under OCPP) and 0 (Ready, no car) are
#   deliberately NOT in the set.
STATUS_CODES_CAR_CONNECTED = frozenset({1, 2, 3, 4, 5, 8})


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Wallbox BLE binary sensors."""
    coordinator: WallboxBleCoordinator = hass.data[DOMAIN][entry.entry_id]
    # OCPP gating approach: AVAILABILITY gating, not creation-time gating. The
    # `ocpp_enabled` flag is populated only after the first *successful* poll
    # (the BLE link connects on a background task, so it is almost never ready
    # during the config-entry first refresh). At this point coordinator.data is
    # typically {} with no `ocpp_enabled` key, so creation-time gating can't
    # tell "confirmed non-OCPP" from "flag not read yet". The Plugged In sensor
    # is therefore always created and suppresses itself via `available`/`is_on`
    # once the flag is known (matching how WallboxOcppBinarySensor reads the
    # flag at runtime).
    async_add_entities(
        [
            WallboxChargingBinarySensor(coordinator),
            WallboxPluggedInBinarySensor(coordinator),
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


class WallboxPluggedInBinarySensor(WallboxBleEntity, BinarySensorEntity):
    """On when a car is connected, derived from the charger status code.

    OCPP-aware via availability gating: plug state cannot be deduced over BLE on
    OCPP-controlled chargers (`r_dat.st` is pinned at 6/Locked), so the sensor
    reports unavailable there instead of a misleading value.
    """

    _attr_name = "Plugged In"
    _attr_device_class = BinarySensorDeviceClass.PLUG

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "plugged_in")

    @property
    def available(self) -> bool:
        # Suppress on OCPP chargers: plug state can't be deduced over BLE
        # (r_dat.st pinned at Locked). Otherwise defer to the coordinator's
        # availability so the sensor still goes unavailable when the link fails.
        if (self.coordinator.data or {}).get("ocpp_enabled"):
            return False
        return super().available

    @property
    def is_on(self) -> bool | None:
        if (self.coordinator.data or {}).get("ocpp_enabled"):
            return None
        code = _status_code(self.coordinator.data or {})
        return None if code is None else code in STATUS_CODES_CAR_CONNECTED


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
