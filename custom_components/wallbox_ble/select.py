"""Select platform for Wallbox BLE — per-slot schedule days.

Each schedule slot (0-3) gets a "Days" select for choosing which days the
schedule runs. The charger stores days as a Sunday-first bitmask
(Sun=1, Mon=2, Tue=4, Wed=8, Thu=16, Fri=32, Sat=64; all days = 127).

The select offers the common presets. For an arbitrary combination that no
preset matches (e.g. Mon+Wed+Fri), the current value shows as "Custom (<mask>)"
and selecting it is a no-op — use the `wallbox_ble.set_schedule` service with a
`days` bitmask for full control.

Setting days only applies to a slot that already has a start/stop; set a time
first on an empty slot.
"""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import WallboxBleCoordinator
from .entity import WallboxBleEntity

_LOGGER = logging.getLogger(__name__)

NUM_SLOTS = 4

# Sunday-first day bits, matching the charger's w_sch encoding.
SUN, MON, TUE, WED, THU, FRI, SAT = 1, 2, 4, 8, 16, 32, 64

# Preset label -> Sunday-first bitmask.
PRESETS: dict[str, int] = {
    "Every day": 127,
    "Weekdays": MON | TUE | WED | THU | FRI,
    "Weekends": SAT | SUN,
    "Monday": MON,
    "Tuesday": TUE,
    "Wednesday": WED,
    "Thursday": THU,
    "Friday": FRI,
    "Saturday": SAT,
    "Sunday": SUN,
}
# Reverse map for showing the current value.
MASK_TO_LABEL: dict[int, str] = {mask: label for label, mask in PRESETS.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up per-slot schedule-days selects."""
    coordinator: WallboxBleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        WallboxScheduleDaysSelect(coordinator, sid) for sid in range(NUM_SLOTS)
    )


class WallboxScheduleDaysSelect(WallboxBleEntity, SelectEntity):
    """Which days one schedule slot runs on."""

    _attr_icon = "mdi:calendar-week"

    def __init__(self, coordinator: WallboxBleCoordinator, sid: int) -> None:
        super().__init__(coordinator, f"schedule_{sid}_days")
        self._sid = sid
        self._attr_name = f"Schedule {sid} Days"
        self._attr_options = list(PRESETS.keys())

    def _slot(self) -> dict | None:
        schedules = (self.coordinator.data or {}).get("schedules") or []
        return next((s for s in schedules if s.get("sid") == self._sid), None)

    @property
    def current_option(self) -> str | None:
        slot = self._slot()
        if not slot:
            return None
        mask = int(slot.get("days", 0)) & 0x7F
        label = MASK_TO_LABEL.get(mask)
        if label is not None:
            return label
        # Arbitrary combination no preset covers: surface it so the value isn't
        # silently blank. Not selectable; use the set_schedule service to change.
        custom = f"Custom ({mask})"
        if custom not in self._attr_options:
            # Expose it as an option so HA renders the current value cleanly.
            self._attr_options = list(PRESETS.keys()) + [custom]
        return custom

    async def async_select_option(self, option: str) -> None:
        mask = PRESETS.get(option)
        if mask is None:
            _LOGGER.debug("Ignoring non-preset days option %r", option)
            return
        await self.coordinator.async_set_schedule_days(self._sid, mask)
