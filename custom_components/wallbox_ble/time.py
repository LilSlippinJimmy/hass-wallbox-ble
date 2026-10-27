"""Time platform for Wallbox BLE — editable per-slot schedule start/stop.

Each schedule slot (0-3) gets a Start and a Stop time entity. The charger
stores schedule times in UTC; these entities present and accept LOCAL time and
convert, using Home Assistant's configured timezone. Setting either writes the
whole slot via the packed w_sch command, preserving the slot's other fields.
"""
from __future__ import annotations

import logging
from datetime import time as dt_time

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

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
    """Set up per-slot start/stop time entities."""
    coordinator: WallboxBleCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[TimeEntity] = []
    for sid in range(NUM_SLOTS):
        entities.append(WallboxScheduleTime(coordinator, sid, is_start=True))
        entities.append(WallboxScheduleTime(coordinator, sid, is_start=False))
    async_add_entities(entities)


def _utc_hhmm_to_local_time(hass: HomeAssistant, hhmm: str) -> dt_time | None:
    """Charger UTC 'HHMM' -> local datetime.time for display."""
    if not hhmm or len(str(hhmm)) < 4:
        return None
    hhmm = str(hhmm).zfill(4)
    tz = dt_util.get_time_zone(hass.config.time_zone)
    base = dt_util.utcnow().replace(
        hour=int(hhmm[:2]), minute=int(hhmm[2:]), second=0, microsecond=0
    )
    local = base.astimezone(tz) if tz else base
    return dt_time(hour=local.hour, minute=local.minute)


def _hhmm_plus_one_minute(hhmm: str) -> str:
    """Return the 'HHMM' one minute after the given 'HHMM' (wraps past 23:59)."""
    h, m = int(hhmm[:2]), int(hhmm[2:])
    total = (h * 60 + m + 1) % (24 * 60)
    return f"{total // 60:02d}{total % 60:02d}"


def _hhmm_minus_one_minute(hhmm: str) -> str:
    """Return the 'HHMM' one minute before the given 'HHMM' (wraps before 00:00)."""
    h, m = int(hhmm[:2]), int(hhmm[2:])
    total = (h * 60 + m - 1) % (24 * 60)
    return f"{total // 60:02d}{total % 60:02d}"


def _local_time_to_utc_hhmm(hass: HomeAssistant, value: dt_time) -> str:
    """Local datetime.time -> charger UTC 'HHMM' for writing."""
    tz = dt_util.get_time_zone(hass.config.time_zone)
    now_local = dt_util.now()
    local_dt = now_local.replace(
        hour=value.hour, minute=value.minute, second=0, microsecond=0
    )
    if tz is not None:
        local_dt = local_dt.replace(tzinfo=tz)
    utc = dt_util.as_utc(local_dt)
    return f"{utc.hour:02d}{utc.minute:02d}"


class WallboxScheduleTime(WallboxBleEntity, TimeEntity):
    """Editable start or stop time for one schedule slot."""

    def __init__(
        self, coordinator: WallboxBleCoordinator, sid: int, *, is_start: bool
    ) -> None:
        key = f"schedule_{sid}_{'start' if is_start else 'stop'}"
        super().__init__(coordinator, key)
        self._sid = sid
        self._is_start = is_start
        self._attr_name = f"Schedule {sid} {'Start' if is_start else 'Stop'}"
        self._attr_icon = "mdi:clock-start" if is_start else "mdi:clock-end"

    def _slot(self) -> dict | None:
        schedules = (self.coordinator.data or {}).get("schedules") or []
        return next((s for s in schedules if s.get("sid") == self._sid), None)

    @property
    def native_value(self) -> dt_time | None:
        slot = self._slot()
        if not slot:
            return None
        hhmm = slot.get("start" if self._is_start else "stop")
        return _utc_hhmm_to_local_time(self.hass, hhmm)

    async def async_set_value(self, value: dt_time) -> None:
        """Write the new time to the charger, preserving the slot's other
        fields (the other time, days, max current)."""
        slot = self._slot() or {}

        # The time being edited (local -> UTC); the other time kept as-is (UTC).
        new_utc = _local_time_to_utc_hhmm(self.hass, value)
        other = slot.get("stop" if self._is_start else "start")

        if other:
            other_utc = str(other).zfill(4)
        elif self._is_start:
            # Setting start on an empty slot: default stop to start + 1 minute
            # (a minimal window) rather than a fixed 23:41.
            other_utc = _hhmm_plus_one_minute(new_utc)
        else:
            # Setting stop on an empty slot: default start to stop - 1 minute.
            other_utc = _hhmm_minus_one_minute(new_utc)

        start_utc = new_utc if self._is_start else other_utc
        stop_utc = other_utc if self._is_start else new_utc

        days = slot.get("days", 127) or 127
        mcr = slot.get("mcr")

        _LOGGER.debug(
            "Set schedule %s %s -> start=%s stop=%s (days=%s)",
            self._sid, "start" if self._is_start else "stop",
            start_utc, stop_utc, days,
        )
        await self.coordinator.async_set_schedule(
            start_utc, stop_utc, sid=self._sid, days=int(days),
            days_are_sunfirst=True, max_current=mcr,
        )
        self.async_write_ha_state()
