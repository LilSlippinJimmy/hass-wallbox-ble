"""Sensor platform for Wallbox BLE."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import WallboxBleCoordinator, status_label
from .entity import WallboxBleEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Wallbox BLE sensors."""
    coordinator: WallboxBleCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        WallboxStatusSensor(coordinator),
        WallboxChargePowerSensor(coordinator),
        WallboxCurrentSensor(coordinator),
        WallboxMaxCurrentSensor(coordinator),
        WallboxScheduleCountSensor(coordinator),
    ]
    # One sensor per schedule slot (the charger supports up to 4).
    entities.extend(WallboxScheduleSlotSensor(coordinator, sid) for sid in range(4))
    async_add_entities(entities)


class WallboxStatusSensor(WallboxBleEntity, SensorEntity):
    """Human-readable charger status, derived from r_dat.st / r_sta.charger_status."""

    _attr_name = "Status"
    _attr_icon = "mdi:ev-station"

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "status")

    @property
    def native_value(self):
        data = self.coordinator.data or {}
        code = (data.get("status") or {}).get("st")
        if code is None:
            code = (data.get("realtime") or {}).get("charger_status")
        return status_label(code)

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        return {
            "status_code": (data.get("status") or {}).get("st"),
            "charger_status": (data.get("realtime") or {}).get("charger_status"),
            "lock_status": (data.get("realtime") or {}).get("lock_status"),
        }


class WallboxChargePowerSensor(WallboxBleEntity, SensorEntity):
    """Instantaneous charge power from r_dat.cp (kW)."""

    _attr_name = "Charge Power"
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "charge_power")

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("status", {}).get("cp")


class WallboxCurrentSensor(WallboxBleEntity, SensorEntity):
    """Charging current setpoint from r_dat.cur (A)."""

    _attr_name = "Charging Current"
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "current")

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("status", {}).get("cur")


class WallboxMaxCurrentSensor(WallboxBleEntity, SensorEntity):
    """Max available current from r_sta.max_available_current (A)."""

    _attr_name = "Max Available Current"
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "max_available_current")

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("realtime", {}).get(
            "max_available_current"
        )


def _fmt_schedule(s: dict) -> dict:
    """Format one raw schedule dict for display/attributes."""
    start, stop = str(s.get("start", "----")), str(s.get("stop", "----"))
    return {
        "sid": s.get("sid"),
        "window": f"{start[:2]}:{start[2:]}-{stop[:2]}:{stop[2:]}",
        "start": start,
        "stop": stop,
        "days": s.get("days"),
        "max_current": s.get("mcr"),
        "enable": s.get("enable"),
        "ulid": s.get("ulid"),
    }


class WallboxScheduleCountSensor(WallboxBleEntity, SensorEntity):
    """Number of schedules configured on the charger, with all of them (every
    slot) exposed as attributes for automations and the UI."""

    _attr_name = "Schedules"
    _attr_icon = "mdi:calendar-multiple"

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "schedules")

    @property
    def native_value(self):
        return len((self.coordinator.data or {}).get("schedules") or [])

    @property
    def extra_state_attributes(self):
        schedules = (self.coordinator.data or {}).get("schedules") or []
        return {
            "schedules": [_fmt_schedule(s) for s in schedules],
            "raw": schedules,
        }


class WallboxScheduleSlotSensor(WallboxBleEntity, SensorEntity):
    """One sensor per schedule slot (0-3). State is the window "HH:MM-HH:MM"
    for that slot, or "none" when the slot is empty. Full fields in attributes.
    """

    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: WallboxBleCoordinator, sid: int) -> None:
        super().__init__(coordinator, f"schedule_{sid}")
        self._sid = sid
        self._attr_name = f"Schedule {sid}"

    def _slot(self) -> dict | None:
        schedules = (self.coordinator.data or {}).get("schedules") or []
        return next((s for s in schedules if s.get("sid") == self._sid), None)

    @property
    def native_value(self):
        s = self._slot()
        if not s:
            return "none"
        start, stop = str(s.get("start", "----")), str(s.get("stop", "----"))
        return f"{start[:2]}:{start[2:]}-{stop[:2]}:{stop[2:]}"

    @property
    def extra_state_attributes(self):
        s = self._slot()
        return _fmt_schedule(s) if s else {}
