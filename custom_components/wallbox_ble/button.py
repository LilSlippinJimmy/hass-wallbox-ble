"""Button platform for Wallbox BLE.

A per-slot "Delete schedule" button for each schedule slot (0-3). Pressing it
removes that slot and compacts the remaining schedules down (this firmware has
no native per-slot delete command — see coordinator.async_delete_schedule).

The button is only *available* when the slot actually holds an active
schedule, so you can't delete an empty slot by accident.
"""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity, ButtonDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import WallboxBleCoordinator
from .entity import WallboxBleEntity

_LOGGER = logging.getLogger(__name__)

NUM_SLOTS = 4


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up per-slot delete buttons and the reboot button."""
    coordinator: WallboxBleCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[ButtonEntity] = [
        WallboxDeleteScheduleButton(coordinator, sid) for sid in range(NUM_SLOTS)
    ]
    entities.append(WallboxRebootButton(coordinator))
    async_add_entities(entities)


class WallboxDeleteScheduleButton(WallboxBleEntity, ButtonEntity):
    """Deletes one schedule slot when pressed."""

    _attr_icon = "mdi:calendar-remove"

    def __init__(self, coordinator: WallboxBleCoordinator, sid: int) -> None:
        super().__init__(coordinator, f"schedule_{sid}_delete")
        self._sid = sid
        self._attr_name = f"Delete Schedule {sid}"

    def _slot(self) -> dict | None:
        schedules = (self.coordinator.data or {}).get("schedules") or []
        return next((s for s in schedules if s.get("sid") == self._sid), None)

    @property
    def available(self) -> bool:
        # Only pressable when the slot holds an active schedule (days set).
        if not super().available:
            return False
        slot = self._slot()
        return bool(slot and int(slot.get("days", 0)) & 0x7F)

    async def async_press(self) -> None:
        _LOGGER.debug("Delete button pressed for schedule slot %s", self._sid)
        await self.coordinator.async_delete_schedule(self._sid)


class WallboxRebootButton(WallboxBleEntity, ButtonEntity):
    """Reboots the charger (rebot). Not gated by OCPP."""

    _attr_name = "Reboot"
    _attr_icon = "mdi:restart"
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "reboot")

    async def async_press(self) -> None:
        _LOGGER.debug("Reboot button pressed")
        await self.coordinator.async_reboot()
