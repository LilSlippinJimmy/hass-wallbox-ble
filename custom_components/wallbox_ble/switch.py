"""Switch platform for Wallbox BLE."""
from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import bapi
from .const import DOMAIN
from .coordinator import WallboxBleCoordinator
from .entity import WallboxBleEntity

_LOGGER = logging.getLogger(__name__)

# w_cha parameter values. Per the Wallbox BAPI convention: 1 = resume/start,
# 2 = pause/stop. Adjust here if a given firmware differs.
CHA_START = "1"
CHA_STOP = "2"

# r_dat.st / r_sta.charger_status code that means actively charging.
STATUS_CHARGING = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Wallbox BLE charging switch."""
    coordinator: WallboxBleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([WallboxChargingSwitch(coordinator)])


class WallboxChargingSwitch(WallboxBleEntity, SwitchEntity):
    """Start/stop charging via the w_cha BAPI command."""

    _attr_name = "Charging"
    _attr_icon = "mdi:ev-station"

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "charging")

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data or {}
        code = (data.get("status") or {}).get("st")
        if code is None:
            code = (data.get("realtime") or {}).get("charger_status")
        try:
            return int(code) == STATUS_CHARGING
        except (ValueError, TypeError):
            return False

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_send(bapi.MET_START_STOP, CHA_START)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_send(bapi.MET_START_STOP, CHA_STOP)
