"""Switch platform for Wallbox BLE.

Only the LED halo standby light is a switch here. The charging control is NOT a
switch (it's a read-only binary_sensor) because charging can't be reliably
forced locally, especially under OCPP — see binary_sensor.py.
"""
from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import WallboxBleCoordinator
from .entity import WallboxBleEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the halo standby switch."""
    coordinator: WallboxBleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([WallboxHaloSwitch(coordinator)])


class WallboxHaloSwitch(WallboxBleEntity, SwitchEntity):
    """LED halo standby light on/off (s_halocfg mode 1/0)."""

    _attr_name = "Halo Standby Light"
    _attr_icon = "mdi:led-on"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "halo_standby")

    @property
    def is_on(self) -> bool | None:
        halo = (self.coordinator.data or {}).get("halo") or {}
        mode = halo.get("mode")
        return None if mode is None else int(mode) == 1

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_halo(mode=1)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_halo(mode=0)
