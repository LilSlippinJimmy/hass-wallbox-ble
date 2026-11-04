"""Wallbox BLE (local) integration.

Phase 2: a DataUpdateCoordinator polls the charger over Bluetooth and exposes
status sensors and a charging switch. Runs alongside the cloud
`wallbox_extended` integration. The read-only `wallbox_ble.probe` service is
kept for diagnostics.

Add from the UI: Settings -> Devices & Services -> Add Integration ->
"Wallbox BLE (Local)" (the charger may also be auto-discovered via Bluetooth).
"""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SERVICE_DELETE_SCHEDULE,
    SERVICE_PROBE,
    SERVICE_SET_SCHEDULE,
    SERVICE_SET_SCHEDULE_FULL,
)
from .coordinator import WallboxBleCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
]

_PROBE_READS = (
    ("r_dat", "main status"),
    ("r_sta", "realtime status"),
    ("r_schs", "schedules"),
    ("r_sn_", "serial number"),
    ("r_lck", "lock state"),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Wallbox BLE from a config entry."""
    # NB: older entries may still carry a stored "pin" key in entry.data; it is
    # intentionally no longer read (the charger has no app-level PIN — it bonds
    # at the OS/Bluetooth level), and tolerating the extra key needs no migration.
    address: str = entry.data[CONF_ADDRESS]
    scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)

    coordinator = WallboxBleCoordinator(hass, address, scan_interval)
    # Start the persistent BLE connection task BEFORE the first refresh so the
    # background loop can bring the link up; the first poll tolerates a
    # not-yet-ready link (serves empty data until the link is established).
    coordinator.start_client()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload the entry when options (scan interval) change, so the new value
    # takes effect immediately without a full restart.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    _register_probe_service(hass)
    _register_schedule_service(hass)
    _register_delete_schedule_service(hass)

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change (new scan interval)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: WallboxBleCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
        # When the last entry unloads, drop the domain-wide services too.
        if not hass.data[DOMAIN]:
            for service in (
                SERVICE_PROBE,
                SERVICE_SET_SCHEDULE,
                SERVICE_SET_SCHEDULE_FULL,
                SERVICE_DELETE_SCHEDULE,
            ):
                if hass.services.has_service(DOMAIN, service):
                    hass.services.async_remove(DOMAIN, service)
    return unloaded


def _register_probe_service(hass: HomeAssistant) -> None:
    """Register the read-only diagnostic probe service (once).

    Reads status/schedules/serial/lock over BLE and logs them. Does not change
    charger state — handy for troubleshooting connectivity.
    """
    if hass.services.has_service(DOMAIN, SERVICE_PROBE):
        return

    async def _handle_probe(call: ServiceCall) -> None:
        # If a specific method is given, call ONLY that (optionally with a raw
        # parameter) and log the result. Otherwise run the default read set.
        # This lets us probe arbitrary BAPI methods for discovery/diagnostics
        # (e.g. testing clr_sch for schedule deletion) without a code change.
        method = call.data.get("method")
        par = call.data.get("par", "null")
        if method:
            reads = ((str(method), f"custom (par={par})"),)
        else:
            reads = _PROBE_READS

        coordinators = list(hass.data.get(DOMAIN, {}).values())
        # Logged at WARNING so results are visible without enabling debug
        # logging (this is a diagnostic service, invoked by hand).
        _LOGGER.warning(
            "Wallbox BLE probe: starting (%d charger(s), method=%s, par=%s)",
            len(coordinators), method or "<default reads>", par,
        )
        if not coordinators:
            _LOGGER.warning(
                "Wallbox BLE probe: no Wallbox BLE coordinators loaded — "
                "is the integration set up?"
            )
            return
        for coordinator in coordinators:
            for met, label in reads:
                try:
                    result = await coordinator.async_send(met, par if method else "null")
                    _LOGGER.warning(
                        "Wallbox BLE probe: %s (%s) -> %s", met, label, result
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning(
                        "Wallbox BLE probe: %s (%s) failed: %s", met, label, err
                    )

    hass.services.async_register(DOMAIN, SERVICE_PROBE, _handle_probe)


def _clean_hhmm(value) -> str:
    """Normalise a time input to a 4-digit 'HHMM' string.

    Accepts "0200", "02:00", 200 (int), or values with stray quotes/spaces,
    and validates the resulting hour/minute. Raises ValueError on bad input.
    """
    s = str(value).strip().strip("\"'").replace(":", "")
    if not s.isdigit():
        raise ValueError(f"time must be digits like '0200', got {value!r}")
    s = s.zfill(4)
    hour, minute = int(s[:2]), int(s[2:])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"time out of range: {value!r}")
    return f"{hour:02d}{minute:02d}"


def _local_hhmm_to_utc(hass: HomeAssistant, hhmm: str) -> str:
    """Convert an 'HHMM' local wall-clock string to the UTC 'HHMM' the charger
    stores, using HA's configured timezone (DST-aware)."""
    from homeassistant.util import dt as dt_util

    hhmm = _clean_hhmm(hhmm)
    hour, minute = int(hhmm[:2]), int(hhmm[2:])
    tz = dt_util.get_time_zone(hass.config.time_zone)
    local_dt = dt_util.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    if tz is not None:
        local_dt = local_dt.replace(tzinfo=tz)
    utc_dt = dt_util.as_utc(local_dt)
    return f"{utc_dt.hour:02d}{utc_dt.minute:02d}"


# Day name -> Monday-first bit (bit0=Mon .. bit6=Sun), matching the convention
# async_set_schedule expects when days_are_sunfirst is False. Accepts 3-letter
# and full names, case-insensitive.
_DAY_NAME_TO_MONFIRST_BIT = {
    "mon": 1, "monday": 1,
    "tue": 2, "tues": 2, "tuesday": 2,
    "wed": 4, "weds": 4, "wednesday": 4,
    "thu": 8, "thur": 8, "thurs": 8, "thursday": 8,
    "fri": 16, "friday": 16,
    "sat": 32, "saturday": 32,
    "sun": 64, "sunday": 64,
}
_DAY_KEYWORDS = {
    "all": 127, "everyday": 127, "every_day": 127, "daily": 127,
    "weekdays": 1 | 2 | 4 | 8 | 16,          # Mon-Fri
    "weekends": 32 | 64,                       # Sat+Sun
    "none": 0,
}


def _parse_days(value) -> int:
    """Parse the service `days` into a Monday-first bitmask (0-127).

    Accepts:
      - an int already (back-compat: Monday-first bitmask, 0-127),
      - a keyword string: 'all'/'everyday'/'daily', 'weekdays', 'weekends', 'none',
      - a comma-separated string of day names: 'mon,wed,fri',
      - a list of day names/keywords: ['mon','wed','fri'].
    Raises ValueError on an unknown token.
    """
    if isinstance(value, bool):
        raise ValueError(f"days must not be a boolean: {value!r}")
    if isinstance(value, int):
        if not 0 <= value <= 127:
            raise ValueError(f"days bitmask out of range (0-127): {value}")
        return value
    # Normalise to a list of tokens.
    if isinstance(value, str):
        tokens = [t.strip().lower() for t in value.split(",") if t.strip()]
    elif isinstance(value, (list, tuple)):
        tokens = [str(t).strip().lower() for t in value if str(t).strip()]
    else:
        raise ValueError(f"days must be an int, string, or list; got {type(value)}")

    mask = 0
    for tok in tokens:
        if tok in _DAY_KEYWORDS:
            mask |= _DAY_KEYWORDS[tok]
        elif tok in _DAY_NAME_TO_MONFIRST_BIT:
            mask |= _DAY_NAME_TO_MONFIRST_BIT[tok]
        else:
            raise ValueError(
                f"unknown day {tok!r} (use mon..sun, or all/weekdays/weekends/none)"
            )
    return mask


_SET_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("start"): cv.string,          # "HHMM"
        vol.Required("stop"): cv.string,           # "HHMM"
        vol.Optional("sid", default=0): vol.All(int, vol.Range(min=0, max=4)),
        # Required: the caller must explicitly choose the days (no "all" default).
        # Accepts an int bitmask (Mon-first), a day-name list, or a keyword
        # string. Parsed to a Monday-first bitmask by _parse_days.
        vol.Required("days"): _parse_days,
        vol.Optional("timezone", default="local"): vol.In(["local", "utc"]),
        # Target exactly one charger (device selector). Optional: when omitted
        # and only one charger is configured, that one is used.
        vol.Optional("device_id"): cv.string,
    }
)


def resolve_coordinator(
    device_id: str | None,
    device_entry_ids: set[str] | None,
    domain_entry_ids: set[str],
    coordinators_by_entry: dict,
):
    """Resolve a service call to exactly one coordinator (pure helper).

    Split out from `_coordinator_for_device` so it can be unit-tested without
    Home Assistant. Arguments:
      - device_id: the device the caller targeted, or None.
      - device_entry_ids: the config entry ids attached to that device (the
        device registry entry's `config_entries`), or None if the device is
        unknown. Ignored when device_id is None.
      - domain_entry_ids: all config entry ids currently loaded for this
        integration (the keys of hass.data[DOMAIN]).
      - coordinators_by_entry: {entry_id: coordinator} for this integration.

    Returns the single matching coordinator. Raises HomeAssistantError with a
    clear message when the target is unknown, foreign, missing, or ambiguous.
    """
    if device_id is not None:
        if device_entry_ids is None:
            raise HomeAssistantError(
                f"Unknown device {device_id!r}; it is not a Wallbox BLE charger."
            )
        matching = device_entry_ids & domain_entry_ids
        if not matching:
            raise HomeAssistantError(
                f"Device {device_id!r} is not a Wallbox BLE charger."
            )
        # A device maps to a single config entry for this integration.
        entry_id = next(iter(matching))
        return coordinators_by_entry[entry_id]

    # No device given: default to the sole charger if there is exactly one.
    if len(coordinators_by_entry) == 1:
        return next(iter(coordinators_by_entry.values()))
    if not coordinators_by_entry:
        raise HomeAssistantError("No Wallbox BLE charger is configured.")
    raise HomeAssistantError(
        "Multiple Wallbox chargers are configured; specify the target device."
    )


def _coordinator_for_device(hass: HomeAssistant, device_id: str | None):
    """Resolve a service `device_id` to its coordinator for this integration.

    Looks the device up in the device registry, maps it to this integration's
    config entry, and returns that entry's coordinator. See `resolve_coordinator`
    for the pure resolution rules (and its unit tests).
    """
    from homeassistant.helpers import device_registry as dr

    coordinators_by_entry = dict(hass.data.get(DOMAIN, {}))
    domain_entry_ids = set(coordinators_by_entry)
    device_entry_ids: set[str] | None = None
    if device_id is not None:
        device = dr.async_get(hass).async_get(device_id)
        device_entry_ids = set(device.config_entries) if device is not None else None
    return resolve_coordinator(
        device_id, device_entry_ids, domain_entry_ids, coordinators_by_entry
    )


def _register_schedule_service(hass: HomeAssistant) -> None:
    """Register the set_schedule service (and its set_schedule_full alias), once.

    Both names run the SAME handler: one atomic BLE write that sets start, stop
    AND days for one slot in a single w_sch — the "do it all in one request"
    path, so an automation doesn't have to poke the per-slot Start and Stop Time
    entities separately (which is what races the link). `set_schedule_full` is a
    self-documenting alias for that behaviour. The write targets exactly one
    charger (the `device_id` field, or the only charger if unspecified).
    """

    async def _handle_set_schedule(call: ServiceCall) -> None:
        data = _SET_SCHEDULE_SCHEMA(dict(call.data))
        # A schedule must have at least one active day (matches the Wallbox app).
        if (int(data["days"]) & 0x7F) == 0:
            raise HomeAssistantError(
                "A schedule must have at least one day. Set 'days' to one or more "
                "days (e.g. [mon, wed, fri]) or a keyword (all, weekdays, weekends)."
            )
        if data["timezone"] == "local":
            start = _local_hhmm_to_utc(hass, data["start"])
            stop = _local_hhmm_to_utc(hass, data["stop"])
        else:
            start = _clean_hhmm(data["start"])
            stop = _clean_hhmm(data["stop"])

        coordinator = _coordinator_for_device(hass, data.get("device_id"))
        await coordinator.async_set_schedule(
            start,
            stop,
            sid=data["sid"],
            days=data["days"],
        )

    for service in (SERVICE_SET_SCHEDULE, SERVICE_SET_SCHEDULE_FULL):
        if not hass.services.has_service(DOMAIN, service):
            hass.services.async_register(
                DOMAIN, service, _handle_set_schedule, schema=_SET_SCHEDULE_SCHEMA
            )


_DELETE_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("sid"): vol.All(int, vol.Range(min=0, max=4)),
        # Target exactly one charger (device selector). Optional: when omitted
        # and only one charger is configured, that one is used.
        vol.Optional("device_id"): cv.string,
    }
)


def _register_delete_schedule_service(hass: HomeAssistant) -> None:
    """Register the delete_schedule service (once).

    This firmware has no per-slot delete BAPI command (clr_sch is rejected), so
    the coordinator deletes by rewriting the schedule table with the target
    slot removed and the rest compacted down — the same net effect the app
    achieves over BLE.
    """
    if hass.services.has_service(DOMAIN, SERVICE_DELETE_SCHEDULE):
        return

    async def _handle_delete_schedule(call: ServiceCall) -> None:
        data = _DELETE_SCHEDULE_SCHEMA(dict(call.data))
        coordinator = _coordinator_for_device(hass, data.get("device_id"))
        await coordinator.async_delete_schedule(data["sid"])

    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_SCHEDULE,
        _handle_delete_schedule,
        schema=_DELETE_SCHEDULE_SCHEMA,
    )
