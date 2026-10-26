"""Sensor platform for Wallbox BLE."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import WallboxBleCoordinator, ocpp_status_label, status_label
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
        WallboxLockStatusSensor(coordinator),
        WallboxBatterySensor(coordinator),
        WallboxOcppStatusSensor(coordinator),
        # Live session.
        WallboxSessionEnergySensor(coordinator),
        WallboxSessionPowerSensor(coordinator),
        WallboxSessionTimeSensor(coordinator),
        WallboxSessionGridEnergySensor(coordinator),
        WallboxSessionGreenEnergySensor(coordinator),
        # Last completed session.
        WallboxLastSessionEnergySensor(coordinator),
        # Diagnostics (static).
        WallboxFirmwareSensor(coordinator),
        WallboxMacSensor(coordinator),
        WallboxSerialSensor(coordinator),
        WallboxTimezoneSensor(coordinator),
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


class WallboxLockStatusSensor(WallboxBleEntity, SensorEntity):
    """Lock status from r_lck (0 = unlocked, 1 = locked). Read-only — the
    lock WRITE (w_lck) is not exposed because it is unreliable / disabled when
    the charger is OCPP-controlled."""

    _attr_name = "Lock Status"
    _attr_icon = "mdi:lock"

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "lock_status")

    @property
    def native_value(self):
        lock = (self.coordinator.data or {}).get("lock")
        if lock is None:
            return None
        try:
            return "locked" if int(lock) == 1 else "unlocked"
        except (ValueError, TypeError):
            return None


# ----- Live session (r_lse) -------------------------------------------------

class WallboxSessionEnergySensor(WallboxBleEntity, SensorEntity):
    """Energy delivered this charging session (r_lse.charged_energy, kWh)."""

    _attr_name = "Session Energy"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "session_energy")

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("session", {}).get("charged_energy")


class WallboxSessionPowerSensor(WallboxBleEntity, SensorEntity):
    """Live charging power (r_lse.charging_power, kW)."""

    _attr_name = "Session Power"
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "session_power")

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("session", {}).get("charging_power")


class WallboxSessionTimeSensor(WallboxBleEntity, SensorEntity):
    """Elapsed charging time this session (r_lse.charging_time, seconds)."""

    _attr_name = "Session Duration"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "session_time")

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("session", {}).get("charging_time")


class WallboxSessionGridEnergySensor(WallboxBleEntity, SensorEntity):
    """Grid energy used this session (r_lse.grid_energy, kWh)."""

    _attr_name = "Session Grid Energy"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "session_grid_energy")

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("session", {}).get("grid_energy")


class WallboxSessionGreenEnergySensor(WallboxBleEntity, SensorEntity):
    """Green (solar) energy used this session (r_lse.green_energy, kWh)."""

    _attr_name = "Session Green Energy"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "session_green_energy")

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("session", {}).get("green_energy")


# ----- Last completed session (r_log) --------------------------------------

class WallboxLastSessionEnergySensor(WallboxBleEntity, SensorEntity):
    """Energy of the last completed session (r_log.tot, reported in Wh)."""

    _attr_name = "Last Session Energy"
    _attr_native_unit_of_measurement = UnitOfEnergy.WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "last_session_energy")

    @property
    def native_value(self):
        # Last-session (r_log) is read as part of the live session when the
        # firmware includes it; exposed here from cached data when present.
        return (self.coordinator.data or {}).get("session", {}).get("last_session_wh")


# ----- Diagnostics (static) ------------------------------------------------

class WallboxFirmwareSensor(WallboxBleEntity, SensorEntity):
    """Charger firmware BUILD number (fw_v_). Static — read once at startup.

    NOTE: over BLE this firmware returns only the internal build integer (e.g.
    181), NOT the dotted marketing version (e.g. 6.7.43) that the Wallbox app
    shows. The dotted version is only available from the Wallbox cloud, which
    this local integration does not use. So we expose the build number, which
    is the accurate value reachable over Bluetooth.
    """

    _attr_name = "Firmware Build"
    _attr_icon = "mdi:chip"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "firmware")

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("fw_version")


class WallboxMacSensor(WallboxBleEntity, SensorEntity):
    """Charger WLAN MAC address (g_mac). Static; disabled by default."""

    _attr_name = "WLAN MAC"
    _attr_icon = "mdi:wifi"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "wlan_mac")

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("wlan_mac")


class WallboxSerialSensor(WallboxBleEntity, SensorEntity):
    """Charger serial number (r_sn_). Static; disabled by default."""

    _attr_name = "Serial Number"
    _attr_icon = "mdi:identifier"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "serial")

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("serial")


class WallboxTimezoneSensor(WallboxBleEntity, SensorEntity):
    """Charger timezone (g_tzn). Static; read once at startup."""

    _attr_name = "Timezone"
    _attr_icon = "mdi:map-clock"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "timezone")

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("timezone")


class WallboxBatterySensor(WallboxBleEntity, SensorEntity):
    """Vehicle state-of-charge (%), from r_lse.soc (falls back to r_dat.soc).

    The charger only knows the car's SoC when the vehicle reports it over the
    charging protocol; many EVs don't. When unavailable this reads None (the
    entity shows 'unknown') rather than a fake value.
    """

    _attr_name = "Battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "battery")

    @property
    def native_value(self):
        data = self.coordinator.data or {}
        soc = (data.get("session") or {}).get("soc")
        if soc is None:
            soc = (data.get("status") or {}).get("soc")
        # Treat a missing / zero-when-unreported value as unknown rather than 0%
        # (0 is the charger's default when the car doesn't report SoC).
        if soc in (None, 0):
            return None
        return soc


class WallboxOcppStatusSensor(WallboxBleEntity, SensorEntity):
    """OCPP backend connection status (r_dat.ocpp, falls back to
    r_sta.ocpp_status).

    This is the live connection state to the OCPP backend (e.g. Octopus
    Intelligent): Connected, Not activated, Connection error, etc. It's
    distinct from the static 'OCPP Enabled' binary sensor, which only reports
    whether OCPP is configured at all.
    """

    _attr_name = "OCPP Status"
    _attr_icon = "mdi:cloud-check-variant"

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "ocpp_status")

    @property
    def native_value(self):
        data = self.coordinator.data or {}
        code = (data.get("status") or {}).get("ocpp")
        if code is None:
            code = (data.get("realtime") or {}).get("ocpp_status")
        return ocpp_status_label(code)
