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
        # Value the user just set, shown immediately so the UI doesn't snap back
        # while the debounced write is pending; cleared once the real read-back
        # from the coordinator reflects it.
        self._optimistic: dt_time | None = None

    def _handle_coordinator_update(self) -> None:
        # A fresh poll/flush read-back has arrived — drop the optimistic value
        # so native_value reflects the charger's real state again.
        self._optimistic = None
        super()._handle_coordinator_update()

    def _slot(self) -> dict | None:
        schedules = (self.coordinator.data or {}).get("schedules") or []
        return next((s for s in schedules if s.get("sid") == self._sid), None)

    @property
    def native_value(self) -> dt_time | None:
        # Show the value the user just set until the real read-back arrives, so
        # a quick double-edit doesn't visibly snap back mid-debounce.
        if self._optimistic is not None:
            return self._optimistic
        slot = self._slot()
        if not slot:
            return None
        hhmm = slot.get("start" if self._is_start else "stop")
        return _utc_hhmm_to_local_time(self.hass, hhmm)

    async def async_set_value(self, value: dt_time) -> None:
        """Stage the new time and let the coordinator coalesce rapid edits.

        Instead of each start/stop entity firing its own full BLE write (which
        race on this charger's flaky link and flip the entity to unavailable),
        we stage JUST the field this entity owns into a per-slot pending buffer
        on the coordinator and reflect the value optimistically. A debounced
        flush then performs ONE atomic w_sch for the slot with both edits
        merged. This never raises on a transient failure — the entity keeps its
        optimistic value and the next successful poll/flush reconciles it.
        """
        # Only the edited field is sent; the other time/days/max-current are
        # resolved at flush time from whatever is staged or last polled. This is
        # what lets start-then-stop merge into one write.
        new_utc = _local_time_to_utc_hhmm(self.hass, value)

        _LOGGER.debug(
            "Stage schedule %s %s -> %s (debounced)",
            self._sid, "start" if self._is_start else "stop", new_utc,
        )
        try:
            self.coordinator.stage_schedule_edit(
                self._sid,
                start=new_utc if self._is_start else None,
                stop=new_utc if not self._is_start else None,
            )
            # Optimistically reflect the edit right away.
            self._optimistic = dt_time(hour=value.hour, minute=value.minute)
        except Exception as err:  # noqa: BLE001
            # Never let a transient staging error push the entity to
            # unavailable; log and keep the last good state.
            _LOGGER.warning(
                "Schedule %s %s edit could not be staged: %s (keeping last state)",
                self._sid, "start" if self._is_start else "stop", err,
            )
        self.async_write_ha_state()
