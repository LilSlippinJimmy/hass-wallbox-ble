"""Binary sensor platform for Wallbox BLE.

Read-only state entities:
  - Charging   : whether the charger is actively charging (was a switch; under
                 OCPP you cannot reliably force-start locally, so this reflects
                 state only).
  - Plugged in : whether a car is connected, derived from the charger status
                 code (realtime `r_sta.charger_status` preferred). Works on all
                 chargers — OCPP and non-OCPP: on an OCPP-locked charger a
                 connected car shows as realtime status 19 (waiting-to-unlock)
                 even though the basic status (`r_dat.st`) stays Locked.
  - OCPP enabled : whether the charger is managed by an OCPP backend. Static;
                   read once at startup.
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

# Status codes (r_sta.charger_status / r_dat.st) that mean a car is connected:
#   1 = Charging, 2 = Connected waiting for car, 3 = Connected waiting for
#   schedule, 4 = Paused, 5 = Schedule ended, 8 = Connected waiting for current
#   assignation, 19 = Waiting-to-unlock (connected while OCPP-locked),
#   20 = Establishing-connection, 21 = Connected low battery.
#   0 (Ready, no car) and 6 (Locked, no car) are deliberately NOT in the set.
STATUS_CODES_CAR_CONNECTED = frozenset({1, 2, 3, 4, 5, 8, 19, 20, 21})


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Wallbox BLE binary sensors."""
    coordinator: WallboxBleCoordinator = hass.data[DOMAIN][entry.entry_id]
    # All three sensors are created unconditionally. The Plugged In sensor now
    # derives plug state from the charger status for every charger (OCPP and
    # non-OCPP), so there is no OCPP gating to apply here.
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


def _plug_status_code(data: dict) -> int | None:
    """Status code for plug detection, preferring the realtime (r_sta)
    status — on OCPP-locked chargers r_dat.st stays at 6 (Locked) while
    r_sta.charger_status moves to 19 (waiting-to-unlock = car connected).
    Falls back to r_dat.st when realtime isn't available."""
    code = (data.get("realtime") or {}).get("charger_status")
    if code is None:
        code = (data.get("status") or {}).get("st")
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
    """On when a car is connected, derived from the charger status code
    (preferring the realtime `r_sta` status).

    Works on OCPP and non-OCPP chargers. On an OCPP-locked charger a connected
    car shows as status 19 (waiting-to-unlock) even though the basic status
    (`r_dat.st`) stays at 6 (Locked).
    """

    _attr_name = "Plugged In"
    _attr_device_class = BinarySensorDeviceClass.PLUG

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "plugged_in")

    @property
    def is_on(self) -> bool | None:
        # CAVEAT: plug detection depends on r_sta responding. The coordinator's
        # resilient poll preserves the previous `realtime` value when an r_sta
        # read fails, so an occasional timeout just holds the last known plug
        # state. If r_sta has NEVER been read (no `realtime` data yet),
        # _plug_status_code falls back to r_dat.st — which on an OCPP charger is
        # 6 (Locked), reading not-plugged until r_sta first arrives. Acceptable.
        code = _plug_status_code(self.coordinator.data or {})
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
