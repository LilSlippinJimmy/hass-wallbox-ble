"""BAPI (Bluetooth API) protocol for Wallbox chargers.

Python port of the framing / checksum / response-parsing logic from
botts7/esp32-wallbox (MIT licensed). The charger speaks JSON over BLE GATT:

Request wire format:
    b"EaE" + length + payload + checksum
  where:
    - length is a single byte if len(payload) < 256
    - otherwise: 0x00 + ASCII-decimal length + 0x00
    - checksum = (sum of every byte before the checksum) & 0xFF

Response: raw JSON (no EaE framing), possibly split across several BLE
notifications (~20-byte MTU chunks). Reassembled by tracking brace depth.
"""
from __future__ import annotations

import json
import logging

_LOGGER = logging.getLogger(__name__)

# Read methods (safe — no state change)
MET_PING = "ping"
MET_GET_STATUS = "r_dat"      # main status (power, current, charge state)
MET_GET_REALTIME = "r_sta"    # realtime status (detailed, lock, phases)
MET_GET_SCHEDULES = "r_schs"  # all schedules
MET_GET_SERIAL = "r_sn_"      # serial number
MET_GET_LOCK_STATE = "r_lck"  # 0=unlocked, 1=locked
MET_GET_LIVE_SESSION = "r_lse"  # live session: charged_energy(kWh),
                                # charging_power(kW), charging_time(s),
                                # grid_energy, green_energy, control_mode, ...
MET_GET_SESSION_LOG = "r_log"   # last completed session: tot/en (Wh), sec, ...
MET_GET_MAX_AVAILABLE = "r_fsI"  # max available current (A), plain int
MET_GET_FW_VERSION = "fw_v_"     # firmware BUILD number (int, e.g. 181).
                                 # The protocol defines a fuller response
                                 # ({db, r=firmware, s=software "6.7.43", ...})
                                 # but on tested Pulsar Plus firmware the BLE
                                 # reply is just the build int; the dotted
                                 # software version is cloud-only.
MET_GET_MAC = "g_mac"            # {eth_mac, wlan_mac}
MET_GET_OCPP = "g_ocpp"          # {chid, e, pw, u} — e=1 means OCPP enabled.
                                 # WARNING: pw/u/chid are SECRETS; never surface
                                 # them as entities. Only the `e` flag is used.
MET_GET_AUTOLOCK = "g_alo"       # auto-lock config (int on this firmware)
MET_GET_TIMEZONE = "g_tzn"       # {timezone: "<tz string>"}
MET_GET_HALO = "g_halocfg"       # LED halo standby: {mode, time_s, bright}
                                 # mode 0=off/1=on, bright 0-100, time_s seconds

# Write methods for optional config.
MET_SET_HALO = "s_halocfg"       # set LED halo: {mode, time_s, bright}
                                 # Must send ALL three fields; read-modify-write
                                 # to preserve the ones not being changed.

# Write methods (state-changing).
MET_START_STOP = "w_cha"      # start/stop/pause/resume charging
MET_SET_CURRENT = "w_mxI"     # set max charging current (plain int amps)
MET_REBOOT = "rebot"          # reboot the charger
MET_SET_ADV_SCHED = "s_sch"   # advanced schedule write — NOT supported on this
                              # firmware ("No dispatch method found").
MET_SET_SCHED = "w_sch"       # packed schedule write (THIS firmware).
# VERIFIED against a Pulsar Plus by batch format-testing (the read-back
# changed only for this exact layout). w_sch takes a PACKED STRING parameter,
# not a JSON object:
#   "<sid:1><startHHMM:4><stopHHMM:4><days:3>"
# - sid is a SINGLE digit (0-based here; slot 0 is the one the charger uses).
#   A 2-DIGIT sid shifts every field by one and the write is rejected (code 7).
# - start/stop are HHMM in UTC.
# - days is a 3-digit Sunday-first bitmask: Sun=1,Mon=2,Tue=4,Wed=8,Thu=16,
#   Fri=32,Sat=64.  All days = 127.
# Example: sid 0, 02:00-02:30, every day -> "002000230127".
# w_sch does NOT acknowledge: a successful write returns resp=None (no reply);
# confirm success by re-reading r_schs. A JSON object returns
# {"error":{"code":1,"message":"Unexpected Error"}}; a wrong-width packed
# string returns {"code":7}.


def _checksum(data: bytes) -> int:
    """Sum of all bytes, truncated to one byte."""
    return sum(data) & 0xFF


def build_command(met: str, par: str = "null", req_id: int = 0) -> str:
    """Build the minimal BAPI command JSON string.

    `par` must already be a JSON fragment: "null", "32",
    '{"pin":"1234"}', etc. — it is inserted verbatim.
    """
    return f'{{"met":"{met}","par":{par},"id":{req_id}}}'


def frame(json_payload: str) -> bytes:
    """Frame a JSON payload into the BAPI wire format."""
    payload = json_payload.encode("utf-8")
    plen = len(payload)

    out = bytearray(b"EaE")
    if plen < 256:
        out.append(plen)
    else:
        out.append(0)
        out += str(plen).encode("ascii")
        out.append(0)
    out += payload

    out.append(_checksum(bytes(out)))
    return bytes(out)


class ResponseParser:
    """Reassembles a JSON response that arrives across BLE notifications.

    Mirrors the brace-depth state machine in the C++ implementation,
    including the resync backstop against a framing desync.
    """

    MAX_BUF = 16384  # hard ceiling; beyond this we've lost sync

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._buf = bytearray()
        self._brace_depth = 0
        self._in_string = False
        self._escape = False

    def feed(self, data: bytes) -> str | None:
        """Feed raw notification bytes. Returns the JSON string once a
        complete brace-balanced object has arrived, else None."""
        for byte in data:
            char = chr(byte)

            if len(self._buf) >= self.MAX_BUF:
                # Lost sync — drop and wait for a fresh object start.
                self.reset()
                if char != "{":
                    continue

            self._buf.append(byte)

            if self._escape:
                self._escape = False
                continue
            if char == "\\" and self._in_string:
                self._escape = True
                continue
            if char == '"':
                self._in_string = not self._in_string
                continue
            if self._in_string:
                continue

            if char == "{":
                self._brace_depth += 1
            elif char == "}":
                self._brace_depth -= 1
                if self._brace_depth == 0 and len(self._buf) > 0:
                    result = self._buf.decode("utf-8", errors="replace")
                    self.reset()
                    return result
                if self._brace_depth < 0:
                    # A '}' with no matching '{': started mid-frame. Resync.
                    self.reset()
        return None

    @staticmethod
    def parse(raw: str) -> dict | None:
        """Parse a complete response string into a dict, or None on error."""
        try:
            return json.loads(raw)
        except (ValueError, TypeError) as err:
            _LOGGER.debug("Failed to parse BAPI response %r: %s", raw, err)
            return None
