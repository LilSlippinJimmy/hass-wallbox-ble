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

NUM_SLOTS = 4

# Days in display order, with each one's Sunday-first bit (the charger's w_sch
# encoding: Sun=1, Mon=2, Tue=4, Wed=8, Thu=16, Fri=32, Sat=64).
DAYS: list[tuple[str, int]] = [
    ("Monday", 2),
    ("Tuesday", 4),
    ("Wednesday", 8),
    ("Thursday", 16),
    ("Friday", 32),
    ("Saturday", 64),
    ("Sunday", 1),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the halo standby switch and per-slot per-day schedule switches."""
    coordinator: WallboxBleCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = [WallboxHaloSwitch(coordinator)]
    for sid in range(NUM_SLOTS):
        for day_name, day_bit in DAYS:
            entities.append(
                WallboxScheduleDaySwitch(coordinator, sid, day_name, day_bit)
            )
    async_add_entities(entities)


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


class WallboxScheduleDaySwitch(WallboxBleEntity, SwitchEntity):
    """One day of the week for one schedule slot.

    Toggling flips just this day's bit in the slot's days bitmask, preserving
    the slot's times, current and other days (read-modify-write). Marked as a
    config entity so the 28 day switches (4 slots x 7 days) collapse into the
    device page's Configuration section instead of cluttering the main view.

    Only meaningful once the slot has a start/stop; toggling a day on an empty
    slot is a no-op (set a time first).
    """

    _attr_icon = "mdi:calendar-today"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, coordinator: WallboxBleCoordinator, sid: int, day_name: str, day_bit: int
    ) -> None:
        super().__init__(coordinator, f"schedule_{sid}_day_{day_name.lower()}")
        self._sid = sid
        self._day_bit = day_bit
        self._attr_name = f"Schedule {sid} {day_name}"

    def _slot(self) -> dict | None:
        schedules = (self.coordinator.data or {}).get("schedules") or []
        return next((s for s in schedules if s.get("sid") == self._sid), None)

    @property
    def available(self) -> bool:
        # Days only apply to a slot that actually has a schedule.
        return super().available and self._slot() is not None

    @property
    def is_on(self) -> bool | None:
        slot = self._slot()
        if not slot:
            return None
        return bool(int(slot.get("days", 0)) & self._day_bit)

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_schedule_day(self._sid, self._day_bit, True)
        # Reflect the charger's actual state (write may be a no-op on an empty
        # slot, or refused if it would clear the last day).
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        result = await self.coordinator.async_set_schedule_day(
            self._sid, self._day_bit, False
        )
        if result is not None and result.get("ok") is False:
            # Refused (e.g. last remaining day) — raise so the UI surfaces it
            # and the toggle snaps back to on.
            from homeassistant.exceptions import HomeAssistantError

            self.async_write_ha_state()
            raise HomeAssistantError(
                f"Schedule {self._sid} must keep at least one day selected. "
                "Enable another day before turning this one off."
            )
        self.async_write_ha_state()
