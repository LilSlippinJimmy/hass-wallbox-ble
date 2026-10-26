"""Number platform for Wallbox BLE — max charging current.

Sets the charger's max charging current (amps) via the w_mxI BAPI command.

NOTE on OCPP: if the charger is OCPP-controlled, the OCPP backend may override
this value during a session. The value you set is still written and read back,
but the effective limit can change again outside Home Assistant's control.
"""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import WallboxBleCoordinator
from .entity import WallboxBleEntity

_LOGGER = logging.getLogger(__name__)

MIN_CURRENT = 6
MAX_CURRENT = 32


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the max charging current and halo brightness numbers."""
    coordinator: WallboxBleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            WallboxMaxCurrentNumber(coordinator),
            WallboxHaloBrightnessNumber(coordinator),
        ]
    )


class WallboxMaxCurrentNumber(WallboxBleEntity, NumberEntity):
    """Settable max charging current (amps)."""

    _attr_name = "Max Charging Current"
    _attr_icon = "mdi:current-ac"
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_native_min_value = MIN_CURRENT
    _attr_native_max_value = MAX_CURRENT
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "max_charging_current")

    @property
    def native_value(self) -> float | None:
        # Reflect the charger's current setpoint (r_dat.cur), which is what the
        # charge is running at; falls back to None if unknown.
        return (self.coordinator.data or {}).get("status", {}).get("cur")

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_max_current(int(value))


class WallboxHaloBrightnessNumber(WallboxBleEntity, NumberEntity):
    """LED halo standby brightness (0-100%), via s_halocfg."""

    _attr_name = "Halo Brightness"
    _attr_icon = "mdi:brightness-6"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "halo_brightness")

    @property
    def native_value(self) -> float | None:
        halo = (self.coordinator.data or {}).get("halo") or {}
        return halo.get("bright")

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_halo(bright=int(value))
