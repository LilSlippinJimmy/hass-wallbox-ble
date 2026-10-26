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
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_PIN,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SERVICE_DELETE_SCHEDULE,
    SERVICE_PROBE,
    SERVICE_SET_SCHEDULE,
)
from .coordinator import WallboxBleCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
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
    address: str = entry.data[CONF_ADDRESS]
    pin: str | None = entry.data.get(CONF_PIN)
    scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)

    coordinator = WallboxBleCoordinator(hass, address, pin, scan_interval)
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
            for service in (SERVICE_PROBE, SERVICE_SET_SCHEDULE, SERVICE_DELETE_SCHEDULE):
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


_SET_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("start"): cv.string,          # "HHMM"
        vol.Required("stop"): cv.string,           # "HHMM"
        vol.Optional("sid", default=0): vol.All(int, vol.Range(min=0, max=4)),
        vol.Optional("days", default=127): vol.All(int, vol.Range(min=0, max=127)),
        vol.Optional("max_current"): vol.All(int, vol.Range(min=6, max=32)),
        vol.Optional("timezone", default="local"): vol.In(["local", "utc"]),
    }
)


def _register_schedule_service(hass: HomeAssistant) -> None:
    """Register the set_schedule service (once)."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_SCHEDULE):
        return

    async def _handle_set_schedule(call: ServiceCall) -> None:
        data = _SET_SCHEDULE_SCHEMA(dict(call.data))
        if data["timezone"] == "local":
            start = _local_hhmm_to_utc(hass, data["start"])
            stop = _local_hhmm_to_utc(hass, data["stop"])
        else:
            start = _clean_hhmm(data["start"])
            stop = _clean_hhmm(data["stop"])

        for coordinator in list(hass.data.get(DOMAIN, {}).values()):
            await coordinator.async_set_schedule(
                start,
                stop,
                sid=data["sid"],
                days=data["days"],
                max_current=data.get("max_current"),
            )

    hass.services.async_register(
        DOMAIN, SERVICE_SET_SCHEDULE, _handle_set_schedule, schema=_SET_SCHEDULE_SCHEMA
    )


_DELETE_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("sid"): vol.All(int, vol.Range(min=0, max=4)),
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
        for coordinator in list(hass.data.get(DOMAIN, {}).values()):
            await coordinator.async_delete_schedule(data["sid"])

    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_SCHEDULE,
        _handle_delete_schedule,
        schema=_DELETE_SCHEDULE_SCHEMA,
    )
