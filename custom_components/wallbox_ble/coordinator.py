"""Data update coordinator for Wallbox BLE."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

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


# OCPP backend connection state, from r_dat.ocpp / r_sta.ocpp_status.
# Values verified from the Wallbox app (enum l41.d2). This is the live
# connection status to the OCPP backend (e.g. Octopus), distinct from the
# static "OCPP enabled" config flag (g_ocpp.e).
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


class WallboxBleCoordinator(DataUpdateCoordinator):
    """Polls the charger over BLE and caches status + schedules."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        pin: str | None,
        scan_interval: int = 15,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {address}",
            update_interval=timedelta(seconds=scan_interval),
        )
        self._address = address
        self._pin = pin
        # The client owns a persistent BLE connection on its own background
        # task (started in __init__.py via start_client); the coordinator is a
        # thin poller that checks `ready` and funnels every op through it.
        self._client = WallboxBleClient(hass, address, pin)
        # Tolerate transient BLE contention (phone app grabbing the single BLE
        # slot) — keep serving the last-known data for a few failed cycles
        # instead of blanking every entity to "unavailable" on one miss.
        self._consecutive_failures = 0
        self._max_tolerated_failures = 3
        # Static info (firmware, MAC, OCPP flag, serial) is read ONCE, the
        # first time the link is ready, then cached and merged into every
        # poll's data. These values don't change at runtime, so re-reading
        # them every cycle would just add BLE traffic for nothing.
        self._static: dict = {}
        self._static_fetched = False

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

        halo = await self._client.request(bapi.MET_GET_HALO)
        if isinstance(halo, dict):
            static["halo"] = halo

        if static:
            self._static = static
            self._static_fetched = True
            _LOGGER.debug("Fetched static Wallbox info: %s", list(static))

    async def _poll_once(self) -> dict | None:
        """One poll attempt. Returns the data dict, or None if the link isn't
        ready / the charger didn't answer. Serialisation lives in the client."""
        if not self._client.ready:
            return None

        # One-time static reads (firmware/MAC/OCPP/serial).
        if not self._static_fetched:
            await self._fetch_static()

        status = await self._client.request(bapi.MET_GET_STATUS)        # r_dat
        realtime = await self._client.request(bapi.MET_GET_REALTIME)    # r_sta
        schedules = await self._client.request(bapi.MET_GET_SCHEDULES)  # r_schs
        session = await self._client.request(bapi.MET_GET_LIVE_SESSION)  # r_lse
        last = await self._client.request(bapi.MET_GET_SESSION_LOG)     # r_log
        lock = await self._client.request(bapi.MET_GET_LOCK_STATE)      # r_lck

        if status is None and realtime is None:
            return None

        serial = self._static.get("serial") or (self.data or {}).get("serial")

        session_data = session if isinstance(session, dict) else {}
        # Fold the last completed session's energy (r_log.tot, in Wh) into the
        # session dict so the "Last Session Energy" sensor can read it.
        if isinstance(last, dict) and last.get("tot") is not None:
            session_data = dict(session_data)
            session_data["last_session_wh"] = last.get("tot")

        return {
            "status": status or {},
            "realtime": realtime or {},
            "schedules": (schedules or {}).get("schedules", []),
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
        # came back empty, keep serving the last-known data for a few cycles
        # (transient BLE contention is normal) before marking entities
        # unavailable.
        if not self._client.ready:
            return self._tolerate_or_fail("BLE link not connected (reconnecting)")

        try:
            result = await self._poll_once()
        except Exception as err:  # noqa: BLE001
            return self._tolerate_or_fail(f"Error polling Wallbox over BLE: {err}")

        if result is not None:
            self._consecutive_failures = 0
            return result

        return self._tolerate_or_fail(
            "No response from charger over BLE (link may have dropped)"
        )

    def _tolerate_or_fail(self, message: str) -> dict:
        """On a failed poll, keep serving the last-known data for a few cycles
        (transient BLE contention is normal) and only raise UpdateFailed — which
        marks entities unavailable — once failures persist."""
        self._consecutive_failures += 1
        # On the very first refresh the link may still be coming up on the
        # background task. Don't fail setup over that — start with empty data
        # and let the first successful poll fill it in.
        if self.data is None:
            _LOGGER.debug("%s (link still establishing; serving empty data)", message)
            return {}
        if self._consecutive_failures <= self._max_tolerated_failures:
            _LOGGER.debug(
                "%s (keeping last data, failure %s/%s)",
                message, self._consecutive_failures, self._max_tolerated_failures,
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

    async def async_set_halo(
        self, *, mode: int | None = None, bright: int | None = None,
        time_s: int | None = None,
    ) -> dict | None:
        """Set the LED halo standby config via s_halocfg.

        s_halocfg requires ALL three fields {mode, time_s, bright}, so this is
        read-modify-write: start from the last-known halo config and override
        only the field(s) given, so changing brightness doesn't wipe the mode
        (and vice-versa). Re-reads afterwards to confirm and refresh entities.
        """
        import json

        current = dict((self.data or {}).get("halo") or self._static.get("halo") or {})
        new = {
            "mode": int(mode if mode is not None else current.get("mode", 1)),
            "time_s": int(time_s if time_s is not None else current.get("time_s", 10)),
            "bright": int(bright if bright is not None else current.get("bright", 100)),
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

    async def async_set_schedule(
        self,
        start: str,
        stop: str,
        *,
        sid: int = 0,
        days: int = 127,
        days_are_sunfirst: bool = False,
        max_current: int | None = None,  # noqa: ARG002 - not carried by w_sch
        enable: bool = True,             # noqa: ARG002 - not carried by w_sch
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
        _LOGGER.debug("Writing schedule w_sch par=%s", par_json)
        # Fire-and-confirm: w_sch sends no reply, so ignore the (None) response
        # and verify by reading the schedules straight back. Both calls run
        # under one lock hold (request_many) so a poll can't interleave between
        # the write and its read-back.
        _, schedules = await self._client.request_many(
            [("w_sch", par_json), (bapi.MET_GET_SCHEDULES, "null")]
        )

        after = (schedules or {}).get("schedules", [])
        # Push the fresh read into the coordinator so entities update at once.
        if self.data is not None:
            new_data = dict(self.data)
            new_data["schedules"] = after
            self.async_set_updated_data(new_data)

        slot = next((s for s in after if s.get("sid") == sid_i), None)
        ok = bool(slot and slot.get("start") == start_s and slot.get("stop") == stop_s)
        if not ok:
            _LOGGER.warning(
                "Schedule write to slot %s may not have applied (read back: %s)",
                sid_i, slot,
            )
        return {"ok": ok, "slot": slot}

    async def async_delete_schedule(self, sid: int) -> dict | None:
        """Delete one schedule slot and compact the remaining ones down.

        This firmware has NO per-slot delete BAPI method: `clr_sch` is rejected
        ("No dispatch method found") in every parameter shape, and `s_sch`/
        advanced-schedule writes are unsupported too. The only working mutation
        is `w_sch` (write one slot by position). So we replicate what the
        Wallbox app does over BLE for a charger without the delete command:
        rewrite the schedule table with the target removed.

        Algorithm (all under ONE lock hold so a poll can't interleave):
          1. Read the current schedules.
          2. Drop the slot with the given sid, keep the rest in order.
          3. Re-write the survivors into positions 0,1,2,... with w_sch, so the
             list is compacted (no gap left where the deleted slot was).
          4. Any trailing position that WAS occupied but now isn't gets cleared
             by writing it with days=0 (verified to leave an inert slot the
             charger never triggers — the closest thing to "empty" w_sch gives).
          5. Read the schedules back and report what remains.

        Times/days of the surviving slots are preserved exactly as the charger
        stored them (UTC HHMM, Sunday-first day bitmask), so we pass them
        through with days_are_sunfirst=True to avoid re-converting.
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

        # 2. Survivors, in their existing order, minus the target.
        survivors = [s for s in before if int(s.get("sid", -1)) != sid_i]

        # 3. Build the compacted rewrite: survivor k -> position k.
        calls: list[tuple[str, str]] = []
        for pos, s in enumerate(survivors):
            start_s = _clean4(s.get("start"))
            stop_s = _clean4(s.get("stop"))
            # Days come back from the charger already Sunday-first; keep as-is.
            sun_first = int(s.get("days", 127)) & 0x7F
            packed = f"{pos:d}{start_s}{stop_s}{sun_first:03d}"
            calls.append(("w_sch", json.dumps(packed)))

        # 4. Clear any trailing positions that were occupied before but are no
        #    longer needed (we now use positions 0..len(survivors)-1). Writing
        #    days=0 leaves an inert slot the charger never fires.
        highest_before = max(occupied_before)
        for pos in range(len(survivors), highest_before + 1):
            packed = f"{pos:d}00000000000"  # start 0000, stop 0000, days 000
            calls.append(("w_sch", json.dumps(packed)))

        # 5. Append a final read-back so everything happens under one lock hold.
        calls.append((bapi.MET_GET_SCHEDULES, "null"))

        _LOGGER.debug(
            "Delete slot %s: rewriting %s survivor(s), clearing positions %s..%s",
            sid_i, len(survivors), len(survivors), highest_before,
        )
        results = await self._client.request_many(calls)
        schedules = results[-1] if results else None

        after = (schedules or {}).get("schedules", [])
        if self.data is not None:
            new_data = dict(self.data)
            new_data["schedules"] = after
            self.async_set_updated_data(new_data)

        # Success = the target sid is gone and we didn't lose a survivor.
        after_active = [s for s in after if int(s.get("days", 0)) & 0x7F]
        gone = all(int(s.get("sid", -1)) != sid_i or not (int(s.get("days", 0)) & 0x7F)
                   for s in after)
        ok = gone and len(after_active) == len(survivors)
        if not ok:
            _LOGGER.warning(
                "Delete slot %s may not have fully applied (read back: %s)",
                sid_i, after,
            )
        return {"ok": ok, "schedules": after}

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self._client.stop()
