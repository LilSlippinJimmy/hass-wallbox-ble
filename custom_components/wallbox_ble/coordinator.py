"""Data update coordinator for Wallbox BLE."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import bapi
from .client import WallboxBleClient
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Charger status codes (index = code), from the BAPI r_dat.st / r_sta.charger_status.
STATUS_CODES = [
    "Ready",                              # 0
    "Charging",                           # 1
    "Connected: waiting for car",         # 2
    "Connected: waiting for schedule",    # 3
    "Paused",                             # 4
    "Schedule ended",                     # 5
    "Locked",                             # 6
    "Error",                              # 7
    "Connected: waiting current",         # 8
    "Unconfigured power sharing",         # 9
    "Queued by Power Boost",              # 10
    "Discharging",                        # 11
    "Connected: waiting MID admin auth",  # 12
    "Connected: MID safety margin",       # 13
    "OCPP unavailable",                   # 14
    "OCPP charge finishing",              # 15
    "OCPP reserved",                      # 16
    "Updating",                           # 17
    "Queued by Eco-Smart",                # 18
]


def status_label(code) -> str | None:
    """Map a numeric status code to a human label."""
    try:
        return STATUS_CODES[int(code)]
    except (ValueError, TypeError, IndexError):
        return f"Unknown ({code})" if code is not None else None


# OCPP backend connection state, from r_dat.ocpp / r_sta.ocpp_status. This is
# the live connection status to the OCPP backend (e.g. Octopus), distinct from
# the static "OCPP enabled" config flag (g_ocpp.e). See docs/ble-protocol.md for
# the status-code tables and their provenance.
OCPP_STATUS_CODES = {
    0: "Not activated",
    1: "Not configured",
    2: "Connection error",
    3: "Connection rejected",
    4: "Connected",            # app label: connection_accepted
    6: "Certificate validation failed",
}


def ocpp_status_label(code) -> str | None:
    """Map a numeric OCPP status code to a human label."""
    try:
        return OCPP_STATUS_CODES.get(int(code), f"Unknown ({code})")
    except (ValueError, TypeError):
        return None


def _clean4(hhmm) -> str:
    """Normalise a time to a 4-digit HHMM string."""
    s = str(hhmm).strip().strip("\"'").replace(":", "")
    return s.zfill(4)[-4:]


def _hhmm_shift(hhmm: str, minutes: int) -> str:
    """Shift a 4-digit 'HHMM' by `minutes` (wraps around the 24h clock)."""
    s = _clean4(hhmm)
    total = (int(s[:2]) * 60 + int(s[2:]) + minutes) % (24 * 60)
    return f"{total // 60:02d}{total % 60:02d}"


# How long to wait after a Time-entity edit before writing the slot, so a
# start edit immediately followed by a stop edit coalesces into ONE w_sch
# write instead of two racing ones.
SCHEDULE_FLUSH_DELAY = 1.2
# How long a user-initiated schedule write will wait for the (frequently
# churning) BLE link to become ready before giving up and deferring.
SCHEDULE_WRITE_READY_TIMEOUT = 9.0
# Pacing gap inserted BETWEEN consecutive w_sch writes when a single operation
# issues several in succession (the delete-and-compact rewrite, which rewrites
# every surviving slot). This charger keeps ONE BLE connection; firing a burst
# of w_sch round-trips back-to-back with no gap tips that single link into a
# drop (observed: the write lands, but the link drops before the read-back, so
# entities briefly go "unknown"). A short gap lets the single link — and the
# poll loop — breathe between writes. This is deliberately GENTLER, not more
# aggressive: no shorter timeouts, no re-pair, no MTU/keepalive change (see
# docs/ble-protocol.md, which traces the prior disconnect storm to exactly that
# kind of aggression). A single lone write (async_set_schedule for one slot) is
# one round-trip and does not need this gap.
SCHEDULE_WRITE_PACING = 0.4

# How long to keep serving the last-known data (entities stay available) through
# continuously-failing polls before marking everything unavailable. A brief BLE
# blip / normal reconnect rides through within this window, so entities do NOT
# flap available -> unavailable -> available and HA does NOT record a fresh
# history segment of the (unchanged) value for every entity at once. A genuine
# sustained outage still goes unavailable once polls fail for longer than this.
TOLERATE_OFFLINE_WINDOW = timedelta(minutes=2)


def merge_schedule_pending(existing: dict | None, update: dict) -> dict:
    """Merge a per-slot pending schedule buffer (pure, HA-free helper).

    Both args are plain dicts describing one schedule slot's intended write.
    `existing` is the buffer already staged for this sid (or None), `update`
    is the new partial edit. Keys: 'start', 'stop', 'days', 'max_current'.
    A value in `update` overrides the existing one ONLY when it is not None;
    a None in `update` leaves the previously-staged value intact. This is what
    lets a start-only edit then a stop-only edit merge into a single
    {start, stop} payload for one atomic w_sch write.

    Returns a NEW dict (never mutates the inputs).
    """
    merged = dict(existing or {})
    for key in ("start", "stop", "days", "max_current"):
        if key in update and update[key] is not None:
            merged[key] = update[key]
    return merged


def plan_delete_compaction(before: list, sid: int) -> list[tuple[int, str, str, int]]:
    """Plan the w_sch rewrites that delete one slot and compact the rest (pure).

    HA-free and side-effect-free so it can be unit-tested without a charger.
    `before` is the schedule list as read back from the charger (each item a
    dict with 'sid','start','stop','days' — days already Sunday-first). `sid` is
    the slot to remove.

    Returns the ordered list of writes to issue, each a tuple
    (pos, start_HHMM, stop_HHMM, days_sunfirst):

      * Surviving slots, in their existing order, renumbered contiguously into
        positions 0,1,2,... so no gap is left where the deleted slot was. Their
        start/stop/days are preserved exactly as the charger stored them.
      * Then one "clear" write (start 0000, stop 0000, days 0) for every trailing
        position that WAS occupied but is no longer needed, so a stale slot can't
        linger past the new compacted tail.

    The returned order IS the order the writes must be issued (and paced). An
    empty list means there is nothing to rewrite.
    """
    occupied_before = {int(s.get("sid")) for s in before if s.get("sid") is not None}
    survivors = [s for s in before if int(s.get("sid", -1)) != int(sid)]

    plan: list[tuple[int, str, str, int]] = []
    for pos, s in enumerate(survivors):
        start_s = _clean4(s.get("start"))
        stop_s = _clean4(s.get("stop"))
        # Days come back from the charger already Sunday-first; keep as-is.
        sun_first = int(s.get("days", 127)) & 0x7F
        plan.append((pos, start_s, stop_s, sun_first))

    # Clear any trailing positions occupied before but no longer needed.
    highest_before = max(occupied_before) if occupied_before else -1
    for pos in range(len(survivors), highest_before + 1):
        plan.append((pos, "0000", "0000", 0))

    return plan


def clr_sch_succeeded(after: list | None, sid: int) -> bool:
    """Decide if a native clr_sch delete took, from the r_schs read-back (pure).

    HA-free so it can be unit-tested without a charger. `after` is the schedule
    list read back from the charger after sending clr_sch (each item a dict with
    at least 'sid', and usually 'days'); `sid` is the slot we asked to delete.

    The delete succeeded when the target slot is GONE — either absent from the
    read-back entirely, or present only as an inert slot (days bitmask 0, which
    the charger never triggers). A None/empty read-back is NOT success (we got no
    confirmation), so this returns False and the caller falls back to the rewrite
    compaction. Note: success is decided HERE, from the read-back, never from the
    (possibly absent) clr_sch ack.
    """
    if not after:
        return False
    for s in after:
        if int(s.get("sid", -1)) == int(sid) and (int(s.get("days", 0)) & 0x7F):
            return False
    return True


def halo_config_known(data: dict | None, static: dict | None) -> bool:
    """Return True when a halo config is known (pure, HA-free helper).

    The halo write (s_halocfg) needs all three fields {mode, time_s, bright},
    so a partial update is only safe when the charger's current halo config has
    already been read. A False result means `async_set_halo` must refuse the
    write (raise HomeAssistantError) rather than inventing hardcoded defaults
    that could silently turn the halo on / reset its timer.
    """
    return bool((data or {}).get("halo") or (static or {}).get("halo"))


class WallboxBleCoordinator(DataUpdateCoordinator):
    """Polls the charger over BLE and caches status + schedules."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        scan_interval: int = 15,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {address}",
            update_interval=timedelta(seconds=scan_interval),
        )
        self._address = address
        # The client owns a persistent BLE connection on its own background
        # task (started in __init__.py via start_client); the coordinator is a
        # thin poller that checks `ready` and funnels every op through it.
        self._client = WallboxBleClient(hass, address)
        # Tolerate transient BLE contention (phone app grabbing the single BLE
        # slot) / brief reconnects — keep serving the last-known data instead of
        # blanking every entity to "unavailable" on a miss. The tolerance is a
        # wall-clock window (TOLERATE_OFFLINE_WINDOW): this marks the start of
        # the current continuous outage (None when the last poll succeeded), so
        # we measure how long polls have been failing in a row rather than
        # counting cycles.
        self._first_failure_at: datetime | None = None
        # Static info (firmware, MAC, OCPP flag, serial) is read ONCE, the
        # first time the link is ready, then cached and merged into every
        # poll's data. These values don't change at runtime, so re-reading
        # them every cycle would just add BLE traffic for nothing.
        self._static: dict = {}
        self._static_fetched = False
        # The halo config is tracked separately from the other static items: a
        # successful serial/fw/mac read must NOT permanently skip a halo read
        # that failed, so halo keeps being retried until it is obtained once.
        self._halo_fetched = False
        # Per-slot debounce for Time-entity edits. Each sid gets a staged
        # {start,stop,days,max_current} buffer and its own flush handle so a
        # start-then-stop double-edit coalesces into ONE w_sch write, and edits
        # to DIFFERENT slots flush independently. Keyed by int sid.
        self._pending_schedules: dict[int, dict] = {}
        self._schedule_flush_handles: dict[int, object] = {}

    @property
    def address(self) -> str:
        return self._address

    def start_client(self) -> None:
        """Start the client's persistent connection task (idempotent)."""
        self._client.start()

    async def _fetch_static(self) -> None:
        """Read the static/rarely-changing info ONCE and cache it.

        firmware version, MAC, OCPP-enabled flag and serial never change at
        runtime, so they are read a single time (the first poll where the link
        is ready) rather than every cycle. A reload/restart re-reads them
        (e.g. after a firmware update). Only the OCPP `e` flag is kept — the
        OCPP URL/password/chargepoint-id are secrets and are discarded here.
        """
        # The genuinely-static items (serial/fw/mac/ocpp/timezone) are read ONCE
        # and never again. The halo config, however, is retried on later polls
        # until it is obtained, because changing only (say) brightness needs the
        # current config and a one-off failed read must not skip it forever.
        if not self._static_fetched:
            static: dict = {}

            serial = await self._client.request(bapi.MET_GET_SERIAL)
            if isinstance(serial, str):
                static["serial"] = serial

            fw = await self._client.request(bapi.MET_GET_FW_VERSION)
            if fw is not None:
                static["fw_version"] = fw

            mac = await self._client.request(bapi.MET_GET_MAC)
            if isinstance(mac, dict):
                static["wlan_mac"] = mac.get("wlan_mac")

            ocpp = await self._client.request(bapi.MET_GET_OCPP)
            if isinstance(ocpp, dict):
                # Keep ONLY the enabled flag. Never store chid/pw/u (secrets).
                static["ocpp_enabled"] = bool(ocpp.get("e"))

            tzinfo = await self._client.request(bapi.MET_GET_TIMEZONE)
            if isinstance(tzinfo, dict):
                static["timezone"] = tzinfo.get("timezone")

            if static:
                # Merge (don't clobber a halo already fetched on an earlier poll).
                self._static.update(static)
                self._static_fetched = True
                _LOGGER.debug("Fetched static Wallbox info: %s", list(static))

        # Halo: retry every call until obtained once (then stop re-reading it).
        if not self._halo_fetched:
            halo = await self._client.request(bapi.MET_GET_HALO)
            if isinstance(halo, dict):
                self._static["halo"] = halo
                self._halo_fetched = True
                _LOGGER.debug("Fetched Wallbox halo config")

    async def _poll_once(self) -> dict | None:
        """One poll attempt. Returns the data dict, or None if the link isn't
        ready / the charger didn't answer. Serialisation lives in the client."""
        if not self._client.ready:
            return None

        # One-time static reads (firmware/MAC/OCPP/serial), plus the halo config
        # which is retried until obtained. _fetch_static guards the genuinely
        # static items internally so they aren't re-read once fetched.
        if not self._static_fetched or not self._halo_fetched:
            await self._fetch_static()

        prev = self.data or {}

        # r_dat is the ONE essential read — status, power, current, ocpp. If it
        # succeeds the cycle is a success, even if the optional reads below
        # fail. This keeps the core status fresh on a marginal/contended link
        # rather than stalling the whole poll because a secondary read timed out
        # (the cause of the status-not-syncing regression when the poll grew to
        # six sequential reads).
        status = await self._client.request(bapi.MET_GET_STATUS)        # r_dat
        if status is None:
            # Core read failed — report a failed cycle so the tolerate/backoff
            # logic handles it, rather than publishing a half-empty update.
            return None

        # Optional reads: best-effort. On failure, keep the previous value for
        # that datum so one flaky read doesn't blank an entity or fail the poll.
        schedules_resp = await self._client.request(bapi.MET_GET_SCHEDULES)  # r_schs
        schedules = (
            (schedules_resp or {}).get("schedules")
            if schedules_resp is not None else prev.get("schedules")
        )

        realtime = await self._client.request(bapi.MET_GET_REALTIME)    # r_sta
        realtime = realtime if realtime is not None else prev.get("realtime")

        session = await self._client.request(bapi.MET_GET_LIVE_SESSION)  # r_lse
        session_data = dict(session) if isinstance(session, dict) else dict(
            prev.get("session") or {}
        )

        last = await self._client.request(bapi.MET_GET_SESSION_LOG)     # r_log
        if isinstance(last, dict) and last.get("tot") is not None:
            session_data["last_session_wh"] = last.get("tot")
        elif "last_session_wh" not in session_data and prev.get("session"):
            session_data["last_session_wh"] = prev["session"].get("last_session_wh")

        lock = await self._client.request(bapi.MET_GET_LOCK_STATE)      # r_lck
        lock = lock if lock is not None else prev.get("lock")

        serial = self._static.get("serial") or prev.get("serial")

        return {
            "status": status or {},
            "realtime": realtime or {},
            "schedules": schedules or [],
            "session": session_data,
            "lock": lock,
            "serial": serial,
            # Cached static values, merged in every cycle so entities always
            # have them even though they're only read once.
            "fw_version": self._static.get("fw_version"),
            "wlan_mac": self._static.get("wlan_mac"),
            "ocpp_enabled": self._static.get("ocpp_enabled"),
            "timezone": self._static.get("timezone"),
            "halo": self._static.get("halo") or {},
        }

    async def _async_update_data(self) -> dict:
        # The client's background task owns reconnection (with back-off), so the
        # poll never drives the link here. If the link isn't ready, or a read
        # came back empty, keep serving the last-known data through a brief
        # outage (transient BLE contention is normal) before marking entities
        # unavailable — see _tolerate_or_fail / TOLERATE_OFFLINE_WINDOW.
        if not self._client.ready:
            return self._tolerate_or_fail("BLE link not connected (reconnecting)")

        try:
            result = await self._poll_once()
        except Exception as err:  # noqa: BLE001
            return self._tolerate_or_fail(f"Error polling Wallbox over BLE: {err}")

        if result is not None:
            # A successful poll ends any ongoing outage window.
            self._first_failure_at = None
            return result

        return self._tolerate_or_fail(
            "No response from charger over BLE (link may have dropped)"
        )

    def _tolerate_or_fail(self, message: str) -> dict:
        """On a failed poll, keep serving the last-known data for up to
        TOLERATE_OFFLINE_WINDOW of continuously-failing polls (a brief BLE blip
        / normal reconnect is normal) and only raise UpdateFailed — which marks
        entities unavailable — once the outage has lasted longer than that. The
        time-based window stops entities flapping available/unavailable on short
        dropouts, which was logging duplicate history segments for every entity
        at once; a genuine sustained outage still goes unavailable."""
        # On the very first refresh the link may still be coming up on the
        # background task. Don't fail setup over that — start with empty data
        # and let the first successful poll fill it in. Do NOT start the outage
        # timer here: there is no last-known data to preserve, and the window
        # must only measure outages that began after we had real data.
        if self.data is None:
            _LOGGER.debug("%s (link still establishing; serving empty data)", message)
            return {}
        now = dt_util.utcnow()
        if self._first_failure_at is None:
            self._first_failure_at = now
        elapsed = now - self._first_failure_at
        if elapsed <= TOLERATE_OFFLINE_WINDOW:
            _LOGGER.debug(
                "%s (keeping last data, offline %ss/%ss)",
                message, int(elapsed.total_seconds()),
                int(TOLERATE_OFFLINE_WINDOW.total_seconds()),
            )
            return self.data
        raise UpdateFailed(message)

    async def _send_with_retry(self, met: str, par: str) -> dict | None:
        """Send one on-demand command through the client's fail-fast path.

        No manual reconnect/retry here: the client serialises the op (so it
        never overlaps a poll — org.bluez "In Progress") and returns None at
        once if the link isn't ready, while its background task reconnects. A
        single attempt is correct — switches/time pickers must not block."""
        return await self._client.request(met, par)

    async def async_send(self, met: str, par: str = "null") -> dict | None:
        """Send a command on demand (used by switches/numbers), then refresh."""
        result = await self._send_with_retry(met, par)
        await self.async_request_refresh()
        return result

    async def async_send_raw(self, met: str, par_obj) -> dict | None:
        """Send a command whose parameter is a JSON object/array, then refresh."""
        import json

        par_json = json.dumps(par_obj, separators=(",", ":"))
        _LOGGER.debug("Sending %s par=%s", met, par_json)
        result = await self._send_with_retry(met, par_json)
        _LOGGER.debug("%s response=%s", met, result)
        await self.async_request_refresh()
        return result

    async def async_set_max_current(self, amps: int) -> dict | None:
        """Set the max charging current (amps) via w_mxI.

        The parameter is a plain integer (e.g. 16, 32). NOTE: if the charger is
        OCPP-controlled, the OCPP backend may override this value during a
        session — the write is still sent and read back, but the effective
        limit can change again outside our control.
        """
        amps_i = int(amps)
        _LOGGER.debug("Setting max current w_mxI=%s", amps_i)
        # w_mxI takes a bare integer parameter.
        await self._client.request(bapi.MET_SET_CURRENT, str(amps_i))
        await self.async_request_refresh()
        return {"ok": True, "amps": amps_i}

    async def async_reboot(self) -> None:
        """Reboot the charger (rebot). Not gated by OCPP."""
        _LOGGER.debug("Rebooting charger (rebot)")
        await self._client.request(bapi.MET_REBOOT)

    async def async_set_schedule_day(
        self, sid: int, day_bit_sunfirst: int, on: bool
    ) -> dict | None:
        """Turn a single day on/off for an existing schedule slot.

        `day_bit_sunfirst` is the Sunday-first bit for one day (Sun=1, Mon=2,
        Tue=4, Wed=8, Thu=16, Fri=32, Sat=64). Reads the slot's current
        days/start/stop/max-current, flips just that bit, and rewrites —
        preserving everything else. No-op if the slot has no schedule yet
        (set a start/stop first).
        """
        schedules = (self.data or {}).get("schedules") or []
        slot = next((s for s in schedules if s.get("sid") == int(sid)), None)
        if not slot:
            _LOGGER.debug("Set day: slot %s has no schedule yet; ignoring", sid)
            return None
        cur = int(slot.get("days", 0)) & 0x7F
        new_days = (cur | day_bit_sunfirst) if on else (cur & ~day_bit_sunfirst)
        # A schedule must always keep at least one active day (matches the
        # Wallbox app). Refuse to turn off the last remaining day.
        if not on and new_days == 0:
            _LOGGER.warning(
                "Refusing to clear the last day of schedule slot %s — a schedule "
                "must have at least one day. Enable another day first.", sid,
            )
            return {"ok": False, "reason": "at_least_one_day_required"}
        start = _clean4(slot.get("start"))
        stop = _clean4(slot.get("stop"))
        return await self.async_set_schedule(
            start, stop, sid=int(sid), days=new_days & 0x7F,
            days_are_sunfirst=True,
        )

    async def async_set_halo(
        self, *, mode: int | None = None, bright: int | None = None,
        time_s: int | None = None,
    ) -> dict | None:
        """Set the LED halo standby config via s_halocfg.

        s_halocfg requires ALL three fields {mode, time_s, bright}, so this is
        read-modify-write: start from the last-known halo config and override
        only the field(s) given, so changing brightness doesn't wipe the mode
        (and vice-versa). Re-reads afterwards to confirm and refresh entities.

        If no halo config is known yet (neither live data nor cached static),
        the write is REFUSED (HomeAssistantError) rather than inventing
        hardcoded defaults — a partial update must never guess the other fields.
        """
        import json

        # Refuse a partial update when the current halo config is unknown: with
        # no known values we'd have to invent defaults, which can silently turn
        # the halo on or reset its timer. Wait until the charger has been read.
        if not halo_config_known(self.data, self._static):
            raise HomeAssistantError(
                "Current halo configuration unknown; cannot safely apply a "
                "partial update yet — try again once the charger has been read."
            )
        current = dict((self.data or {}).get("halo") or self._static.get("halo") or {})
        new = {
            "mode": int(mode if mode is not None else current.get("mode")),
            "time_s": int(time_s if time_s is not None else current.get("time_s")),
            "bright": int(bright if bright is not None else current.get("bright")),
        }
        par = json.dumps(new, separators=(",", ":"))
        _LOGGER.debug("Setting halo s_halocfg=%s", par)
        await self._client.request(bapi.MET_SET_HALO, par)

        # Read back and update cached static + current data so entities reflect it.
        halo = await self._client.request(bapi.MET_GET_HALO)
        if isinstance(halo, dict):
            self._static["halo"] = halo
            if self.data is not None:
                nd = dict(self.data)
                nd["halo"] = halo
                self.async_set_updated_data(nd)
        return halo

    def stage_schedule_edit(
        self,
        sid: int,
        *,
        start: str | None = None,
        stop: str | None = None,
        days: int | None = None,
        max_current: int | None = None,
    ) -> None:
        """Stage a partial schedule edit for one slot and (re)arm a debounced
        flush, so a start edit immediately followed by a stop edit coalesces
        into ONE atomic w_sch write instead of two racing ones.

        Called from the per-slot Time entities' async_set_value. start/stop are
        charger-UTC 'HHMM' strings; days (if given) is a Sunday-first bitmask —
        the flush always writes days Sunday-first (days_are_sunfirst=True), the
        form the charger stores and reads back. Any field
        left None keeps whatever was previously staged for this sid (see
        merge_schedule_pending). Edits to different sids are buffered and flushed
        independently.
        """
        sid_i = int(sid)
        update = {
            "start": _clean4(start) if start is not None else None,
            "stop": _clean4(stop) if stop is not None else None,
            "days": (int(days) & 0x7F) if days is not None else None,
            "max_current": max_current,
        }

        # The pending-buffer merge and timer (re)arm touch loop-owned state
        # (_pending_schedules, _schedule_flush_handles) and call loop-only HA
        # APIs (async_call_later, and async_create_task inside _fire). All of
        # that MUST run on the event loop. async_set_value is already on the
        # loop, but this method can also be reached from an executor/SyncWorker
        # thread, where calling these @callback/async_ APIs directly raises a
        # RuntimeError and the flush coroutine is never awaited. So if we're not
        # on the loop, marshal the whole staging body onto it thread-safely via
        # loop.call_soon_threadsafe; if we are on the loop, run it inline so the
        # debounce/coalesce timing is unchanged.
        try:
            on_loop = asyncio.get_running_loop() is self.hass.loop
        except RuntimeError:
            on_loop = False

        if on_loop:
            self._stage_schedule_edit_on_loop(sid_i, update)
        else:
            self.hass.loop.call_soon_threadsafe(
                self._stage_schedule_edit_on_loop, sid_i, update
            )

    def _stage_schedule_edit_on_loop(self, sid_i: int, update: dict) -> None:
        """Merge a staged edit and (re)arm the per-sid debounced flush.

        MUST run on the event loop: it mutates loop-owned buffers and calls the
        loop-only async_call_later / async_create_task APIs. Callers marshal
        here from off-loop threads via loop.call_soon_threadsafe.
        """
        from homeassistant.helpers.event import async_call_later

        self._pending_schedules[sid_i] = merge_schedule_pending(
            self._pending_schedules.get(sid_i), update
        )

        # Cancel any armed flush for THIS sid and re-arm, so a rapid second edit
        # resets the timer and only one write fires with both new values. Other
        # sids' timers are untouched (independent flush).
        handle = self._schedule_flush_handles.pop(sid_i, None)
        if handle is not None:
            handle()  # async_call_later returns a cancel callback

        def _fire(_now) -> None:
            # Dispatch the flush via hass.add_job rather than async_create_task.
            # add_job is thread-safe: it schedules the coroutine on the event
            # loop regardless of which thread invokes the callback. Observed on
            # HA 2026 / Python 3.14: this timer callback can run in an executor
            # thread, where the loop-only async_create_task raises a hard
            # RuntimeError and the flush coroutine is never awaited (so a staged
            # schedule edit silently reverts to "unknown"). add_job avoids that.
            self._schedule_flush_handles.pop(sid_i, None)
            self.hass.add_job(self._flush_schedule, sid_i)

        self._schedule_flush_handles[sid_i] = async_call_later(
            self.hass, SCHEDULE_FLUSH_DELAY, _fire
        )

    async def _flush_schedule(self, sid: int) -> dict | None:
        """Write the staged (merged) edit for one slot as a single w_sch.

        Pops the pending buffer for `sid` up front so a new edit arriving while
        this flush is in flight stages fresh and re-arms its own timer rather
        than being dropped. If the buffer has no start/stop (nothing writable),
        it is a no-op.
        """
        pending = self._pending_schedules.pop(int(sid), None)
        if not pending:
            return None

        # Resolve start/stop: prefer staged, else the slot's current value from
        # the last poll, else a minimal 1-minute window off whichever we have.
        slot = next(
            (s for s in (self.data or {}).get("schedules", [])
             if s.get("sid") == int(sid)),
            None,
        ) or {}
        start = pending.get("start") or (
            _clean4(slot.get("start")) if slot.get("start") else None
        )
        stop = pending.get("stop") or (
            _clean4(slot.get("stop")) if slot.get("stop") else None
        )
        if start is None and stop is None:
            _LOGGER.debug("Flush slot %s: nothing to write", sid)
            return None
        if start is None:
            start = _hhmm_shift(stop, -1)
        if stop is None:
            stop = _hhmm_shift(start, +1)

        days = pending.get("days")
        if days is None:
            days = int(slot.get("days", 127) or 127) & 0x7F

        return await self.async_set_schedule(
            start, stop, sid=int(sid), days=int(days) & 0x7F,
            days_are_sunfirst=True,
        )

    async def async_set_schedule(
        self,
        start: str,
        stop: str,
        *,
        sid: int = 0,
        days: int = 127,
        days_are_sunfirst: bool = False,
    ) -> dict | None:
        """Write/replace one charging schedule slot using the legacy packed
        w_sch format this firmware uses.

        w_sch does NOT take a JSON object. Its parameter is a packed STRING:
            "<sid:1><startHHMM:4><stopHHMM:4><days:3>"   (verified on this charger)
        sid is a SINGLE digit (slot 0 is the one the charger uses); a 2-digit
        sid shifts every field and the write is rejected. Times are HHMM (UTC);
        days is a 3-digit Sunday-first bitmask (Sun=1,Mon=2,Tue=4,Wed=8,Thu=16,
        Fri=32,Sat=64; all days = 127). Power/energy and an enabled flag are not
        carried by w_sch. w_sch sends no ack (response is None); success is
        confirmed by reading the schedule back.

        start/stop are "HHMM" strings already in the charger's timezone (UTC) —
        the caller handles local->UTC conversion. `days` is given Monday-first
        (bit0=Mon..bit6=Sun) and converted to the charger's Sunday-first bitmask
        here.
        """
        # The charger stores days Sunday-first (Sun=1,Mon=2,...Sat=64). The
        # service takes Monday-first (bit0=Mon) for human convenience and
        # converts; but callers preserving the slot's existing days (read back
        # from the charger) already have Sunday-first and set days_are_sunfirst
        # to skip the conversion (which would otherwise corrupt day subsets).
        if days_are_sunfirst:
            sun_first = int(days) & 0x7F
        else:
            mon_first = int(days) & 0x7F
            sun_first = 0
            weekday_to_sunfirst_bit = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 0}
            for i in range(7):
                if mon_first & (1 << i):
                    sun_first |= 1 << weekday_to_sunfirst_bit[i]

        # VERIFIED against this charger: sid is a SINGLE digit, then start(4),
        # stop(4), days(3 Sunday-first). A 2-digit sid shifts every field and
        # the write is rejected (code 7). w_sch does NOT ack (resp is None);
        # success is confirmed by re-reading the schedule, not by the response.
        sid_i = int(sid)
        start_s = _clean4(start)
        stop_s = _clean4(stop)
        packed = f"{sid_i:d}{start_s}{stop_s}{sun_first:03d}"

        import json

        par_json = json.dumps(packed)  # quoted JSON string

        # USER-WRITE PATH (unlike polling's fail-fast): a schedule write is user
        # initiated and must not be lost just because the link is mid-reconnect.
        # Give the link a short grace period to become ready. On timeout, don't
        # crash or mark the entity unavailable — report a deferral the caller
        # (Time entity / service) can surface quietly.
        if not self._client.ready:
            ready = await self._client.wait_ready(SCHEDULE_WRITE_READY_TIMEOUT)
            if not ready:
                _LOGGER.warning(
                    "Schedule write to slot %s deferred: BLE link not ready "
                    "(will reconcile on next successful poll)", sid_i,
                )
                return {"ok": False, "reason": "link_not_ready", "slot": None}

        _LOGGER.debug("Writing schedule w_sch par=%s", par_json)
        # Fire-and-confirm: w_sch sends no reply, so ignore the (None) response
        # and verify by reading the schedules straight back. Both calls run
        # under one lock hold (request_many) so a poll can't interleave between
        # the write and its read-back.
        _, schedules = await self._client.request_many(
            [("w_sch", par_json), (bapi.MET_GET_SCHEDULES, "null")]
        )

        after = (schedules or {}).get("schedules", [])
        slot = next((s for s in after if s.get("sid") == sid_i), None)
        ok = bool(slot and slot.get("start") == start_s and slot.get("stop") == stop_s)

        # Retry ONCE: if the read-back was empty but the link is still ready, the
        # write may have landed in a brief wobble. Re-issue the same atomic
        # write+read before declaring it un-applied.
        if not ok and self._client.ready:
            _LOGGER.debug(
                "Schedule slot %s read back empty; retrying write once", sid_i
            )
            await asyncio.sleep(0.5)
            _, schedules = await self._client.request_many(
                [("w_sch", par_json), (bapi.MET_GET_SCHEDULES, "null")]
            )
            after = (schedules or {}).get("schedules", [])
            slot = next((s for s in after if s.get("sid") == sid_i), None)
            ok = bool(
                slot and slot.get("start") == start_s and slot.get("stop") == stop_s
            )

        # Push the fresh read into the coordinator so entities update at once.
        # Only when we actually got schedules back (don't blank them on a miss).
        if after and self.data is not None:
            new_data = dict(self.data)
            new_data["schedules"] = after
            self.async_set_updated_data(new_data)

        if not ok:
            if not self._client.ready:
                # The link dropped during the write/read-back. The user confirms
                # these writes DO land on the charger; the read-back just didn't
                # survive the transient drop. Treat this as a transient event:
                # reconcile on the next successful poll instead of logging a
                # scary "may not have applied" warning as if the write failed.
                _LOGGER.debug(
                    "Schedule write to slot %s: link dropped before read-back "
                    "could confirm; will reconcile on next poll", sid_i,
                )
                self.hass.add_job(self.async_request_refresh)
            else:
                # Link is up but the read-back genuinely disagrees — worth a warn.
                _LOGGER.warning(
                    "Schedule write to slot %s may not have applied (read back: %s)",
                    sid_i, slot,
                )
        return {"ok": ok, "slot": slot}

    async def async_delete_schedule(self, sid: int) -> dict | None:
        """Delete one schedule slot — NATIVE clr_sch first, rewrite as fallback.

        The official Wallbox app deletes a single slot over BLE with a dedicated
        command: method `clr_sch` with par `{"sid":[<id>]}` (sid is a JSON ARRAY
        of integer slot ids; one element to delete one slot). This is a single
        round-trip and, unlike the compaction rewrite below, does not fire a
        burst of w_sch writes that destabilises this charger's single BLE link.

        Because a PRIOR attempt in this integration concluded clr_sch was
        unsupported (it used the WRONG parameter shape — a scalar sid, never the
        array form), clr_sch support on this firmware is unproven until the user
        field-tests it. So this is TRY-NATIVE-THEN-FALLBACK:

          1. Send clr_sch with par {"sid":[sid]} and read r_schs back in ONE lock
             hold (request_many — same atomic write+read-back pattern w_sch uses).
          2. If the read-back confirms the slot is GONE -> success. Publish the
             fresh schedules and return. clr_sch may send NO ack (like w_sch), so
             success is decided by the read-back (clr_sch_succeeded), NEVER by the
             response.
          3. If clr_sch did NOT take (slot still present, link dropped, or an
             explicit error / code 7 / "No dispatch method found" response) ->
             FALL BACK to the rewrite-the-whole-table compaction below.

        The fallback is kept intact so delete still works if clr_sch is rejected.
        """
        sid_i = int(sid)

        # 1. NATIVE attempt: clr_sch {"sid":[sid]} + r_schs read-back, atomically.
        if self._client.ready:
            par = bapi.build_clr_sch_par(sid_i)
            _LOGGER.debug("Deleting slot %s natively: clr_sch par=%s", sid_i, par)
            clr_resp, schedules = await self._client.request_many(
                [(bapi.MET_DEL_SCHED, par), (bapi.MET_GET_SCHEDULES, "null")]
            )
            after = (schedules or {}).get("schedules", []) if schedules else []
            # Success is read-back driven, not ack driven. clr_resp is logged
            # only for diagnostics (it may be None, or an error object).
            if clr_sch_succeeded(after, sid_i):
                _LOGGER.debug(
                    "Native clr_sch deleted slot %s (resp=%s)", sid_i, clr_resp
                )
                if after and self.data is not None:
                    new_data = dict(self.data)
                    new_data["schedules"] = after
                    self.async_set_updated_data(new_data)
                return {"ok": True, "schedules": after, "method": "clr_sch"}
            _LOGGER.debug(
                "Native clr_sch did not delete slot %s (resp=%s, read back: %s); "
                "falling back to rewrite-the-table compaction",
                sid_i, clr_resp, after,
            )

        # 2. FALLBACK: rewrite-the-whole-table compaction (the proven path).
        return await self._rewrite_delete_schedule(sid_i)

    async def _rewrite_delete_schedule(self, sid: int) -> dict | None:
        """Fallback delete: rewrite the schedule table with the target removed.

        Used when native clr_sch is rejected or doesn't take. The only working
        mutation on such firmware is `w_sch` (write one slot by position), so we
        replicate what the Wallbox app does for a charger without the delete
        command: rewrite the surviving slots compacted down.

        Algorithm:
          1. Read the current schedules.
          2. Drop the slot with the given sid, keep the rest in order
             (plan_delete_compaction — pure, unit-tested).
          3. Re-write the survivors into positions 0,1,2,... with w_sch, so the
             list is compacted (no gap left where the deleted slot was).
          4. Any trailing position that WAS occupied but now isn't gets cleared
             by writing it with days=0 (verified to leave an inert slot the
             charger never triggers — the closest thing to "empty" w_sch gives).
          5. Read the schedules back and report what remains.

        PACING (stability fix): each w_sch is issued as its OWN atomic write
        round-trip, with a SCHEDULE_WRITE_PACING gap between consecutive writes.
        Previously the whole burst (every survivor + every trailing clear + a
        read-back) was issued inside ONE unbroken request_many lock hold with no
        yield, which hammered this charger's single BLE link and dropped it
        mid-operation — the writes landed, but the link fell over before the
        read-back, so entities briefly went "unknown". Releasing the lock and
        sleeping between writes gives the single link (and the poll loop)
        breathing room. This is GENTLER, not more aggressive (no timeout/pairing
        changes) — see docs/ble-protocol.md. The outcome is unchanged: the same
        compacted rewrite is sent in the same order.

        Times/days of the surviving slots are preserved exactly as the charger
        stored them (UTC HHMM, Sunday-first day bitmask).
        """
        import json

        sid_i = int(sid)

        # 1. Snapshot current schedules.
        current = await self._client.request(bapi.MET_GET_SCHEDULES)
        before = list((current or {}).get("schedules", []))
        if not before:
            _LOGGER.debug("Delete slot %s: no schedules present", sid_i)
            return {"ok": True, "schedules": []}

        occupied_before = {int(s.get("sid")) for s in before if s.get("sid") is not None}
        if sid_i not in occupied_before:
            _LOGGER.debug("Delete slot %s: slot not present, nothing to do", sid_i)
            return {"ok": True, "schedules": before}

        # 2+3+4. Plan the compacted rewrite (survivors renumbered contiguously,
        # plus clears for trailing positions). Pure helper — unit-tested.
        plan = plan_delete_compaction(before, sid_i)
        survivor_count = sum(1 for p in plan if p[3] & 0x7F)

        _LOGGER.debug(
            "Delete slot %s: rewriting %s survivor(s), %s write(s) total, paced "
            "%.2fs apart",
            sid_i, survivor_count, len(plan), SCHEDULE_WRITE_PACING,
        )

        # Issue each w_sch as its OWN atomic round-trip, pacing between them so
        # the single BLE link isn't hammered by the whole burst at once. The
        # lock is released between writes, so a poll can slip in and the link
        # gets breathing room — the fix that stops the delete dropping the link.
        for i, (pos, start_s, stop_s, sun_first) in enumerate(plan):
            if i > 0:
                await asyncio.sleep(SCHEDULE_WRITE_PACING)
            packed = f"{pos:d}{start_s}{stop_s}{sun_first:03d}"
            await self._client.request("w_sch", json.dumps(packed))

        # 5. Read the schedules back (separate round-trip, after a final gap so
        #    the read-back doesn't ride the heels of the last write).
        if plan:
            await asyncio.sleep(SCHEDULE_WRITE_PACING)
        schedules = await self._client.request(bapi.MET_GET_SCHEDULES)

        after = (schedules or {}).get("schedules", [])
        if after and self.data is not None:
            # Only push a fresh read when we actually got one back; don't blank
            # the schedules if the read-back missed because of a transient drop.
            new_data = dict(self.data)
            new_data["schedules"] = after
            self.async_set_updated_data(new_data)

        # Success = the target sid is gone and we didn't lose a survivor.
        after_active = [s for s in after if int(s.get("days", 0)) & 0x7F]
        gone = all(int(s.get("sid", -1)) != sid_i or not (int(s.get("days", 0)) & 0x7F)
                   for s in after)
        ok = gone and len(after_active) == survivor_count

        if not ok:
            if not self._client.ready:
                # The link dropped mid-delete (the write(s) landed, but the
                # read-back couldn't complete). This is NOT a failure — the next
                # successful poll reconciles the real schedule state. Ask for a
                # refresh once the link is back rather than logging a scary
                # warning for what is a transient drop.
                _LOGGER.debug(
                    "Delete slot %s: link dropped before read-back could confirm; "
                    "will reconcile on next poll", sid_i,
                )
                self.hass.add_job(self.async_request_refresh)
            else:
                # Link is up but the read-back genuinely disagrees — worth a warn.
                _LOGGER.warning(
                    "Delete slot %s may not have fully applied (read back: %s)",
                    sid_i, after,
                )
        return {"ok": ok, "schedules": after}

    async def async_shutdown(self) -> None:
        # Cancel any armed schedule-flush timers so they don't fire after the
        # link is gone.
        for handle in list(self._schedule_flush_handles.values()):
            with contextlib.suppress(Exception):
                handle()
        self._schedule_flush_handles.clear()
        self._pending_schedules.clear()
        await super().async_shutdown()
        await self._client.stop()
