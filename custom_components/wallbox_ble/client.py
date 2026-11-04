"""BLE client for a Wallbox Pulsar charger using Home Assistant's Bluetooth stack.

Speaks the BAPI protocol over the charger's custom UART-style GATT service.
The Pulsar Plus requires an encrypted (bonded) BLE link, which only completes
when a NoInputNoOutput BlueZ agent is registered to auto-accept the pairing
authorization (a bare client.pair() yields org.bluez.Error.AuthenticationFailed
because no agent answers the Just-Works authorization request).

This is the integration's own implementation. The pairing-agent registration
and the connection loop are derived from first-party API references — the BlueZ
D-Bus agent API (`org.bluez.AgentManager1` / `org.bluez.Agent1`), the dbus-fast
library's ServiceInterface/@method export API, and the bleak /
bleak-retry-connector connect lifecycle — not from any third-party integration.
The GATT UUIDs and BAPI protocol facts below are verified against this charger.
See docs/ble-protocol.md for the connection/encryption model this code follows.

Connection model (persistent):
  A single background asyncio task (`_run`) owns the BLE link for the lifetime
  of the integration. Each cycle it resolves a FRESH BLEDevice from HA's
  Bluetooth stack, connects, registers the pairing agent + pairs, subscribes to
  the TX notify characteristic, then BLOCKS on an asyncio.Event until the link
  drops. A `disconnected_callback` fires that event the instant BlueZ reports a
  drop; a per-op timeout also fires it (the zombie-link signal, where bleak's
  `is_connected` lies True on a half-dead GATT link). On wake the cycle tears
  the link down cleanly (implicit stop_notify + disconnect) and reconnects with
  exponential back-off (floor 5s, cap 120s), resetting the back-off only after
  a successful connect AND first successful read.

  Requests fail fast: `request()` returns None immediately when the link is not
  ready instead of blocking or raising, so polls and on-demand commands never
  hammer a dead characteristic. RX is decoupled via an asyncio.Queue fed by the
  notify callback and matched on the BAPI request `id`.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from . import bapi

_LOGGER = logging.getLogger(__name__)

WALLBOX_SERVICE_UUID = "331a36f5-2459-45ea-9d95-6142f0c4b307"
UART_RX_CHAR_UUID = "a9da6040-0823-4995-94ec-9ce41ca28833"  # write to charger
UART_TX_CHAR_UUID = "a73e9a10-628f-4494-a099-12efaf72258f"  # notify from charger
# The "mode" characteristic. Reading this protected characteristic triggers
# on-demand SMP encryption/bonding, instead of forcing a bond up front: a
# successful read means the link is encrypted and usable, and we never force a
# re-pair when this read succeeds. See docs/ble-protocol.md for the model.
MODE_CHAR_UUID = "75a9f022-af03-4e41-b4bc-9de90a47d50b"  # protected; read => encrypt

# Per-operation timeout on a single write/read round-trip. A tight timeout turns
# any slow reply into a full reconnect (and, with a force-pair path, another
# re-pair) — a disconnect storm. So we give a single op ~8s, and on timeout
# RETRY the op once before declaring the link dead (see OP_RETRIES). Only after
# the retry also times out do we force a reconnect. See docs/ble-protocol.md.
OP_TIMEOUT = 8.0
# Number of extra attempts for a single op after the first times out, before the
# link is declared a zombie. 1 => up to two attempts total.
OP_RETRIES = 1
# Whole-connection bring-up watchdog. Bounds the connect + encrypt + subscribe
# sequence so a stuck bring-up recycles instead of hanging the background task
# forever. This is a bring-up liveness bound, NOT a per-op value — the two are
# deliberately decoupled. See docs/ble-protocol.md.
CONNECT_WATCHDOG = 15.0

# Exponential reconnect back-off bounds.
BACKOFF_FLOOR = 5.0
BACKOFF_CAP = 120.0


# --- Pure helper: classify a BlueZ/bleak error from the mode-char read -------
# Kept HA-free and side-effect-free so it can be unit-tested without bleak or a
# charger. It decides what a failed protected-characteristic read means, mapping
# GATT status handling onto the string errors BlueZ/bleak surface in HA
# (see docs/ble-protocol.md):
#
#   "encryption_needed" — the read failed because the link is not yet encrypted
#       / not authenticated (GATT 5 InsufficientAuthentication, 15/143
#       InsufficientEncryption, or BlueZ "Not paired"/"Not authorized"
#       equivalents). The app's recovery for these is to bond and retry, so when
#       the device is NOT already bonded we do a single fallback bond.
#   "already_authorized" — GATT 8 InsufficientAuthorization and look-alikes
#       ("already exists", "AuthenticationFailed"). On an ALREADY-bonded device
#       these mean "the bond is there, stop trying to re-pair"; they are
#       NON-FATAL — we continue to subscribe. Forcing a re-pair here is exactly
#       what produced the `Insufficient authorization (8)` drop.
#   "fatal" — anything else (disconnected, GATT error we don't recognise): let
#       the connect cycle treat it as a failure and recycle the link.

_ENCRYPTION_NEEDED_MARKERS = (
    "insufficient authentication",
    "insufficient encryption",
    "not paired",
    "not authenticated",
    "encryption",
    "le-connection-parameter",  # transient; treat as needs-retry-via-bond
)
_ALREADY_AUTHORIZED_MARKERS = (
    "insufficient authorization",
    "authorization",
    "already exists",
    "authenticationfailed",
    "authentication failed",
    "org.bluez.error.alreadyexists",
)


def classify_mode_read_error(err: BaseException | str) -> str:
    """Classify a mode-char read failure. Pure; returns one of
    'encryption_needed' | 'already_authorized' | 'fatal'.

    Takes an exception or a raw message string. Matching is case-insensitive on
    the text of the error (BlueZ/bleak expose these as string messages, not
    typed codes, through HA's stack), with the GATT numeric codes (5/8/15/143)
    also recognised when present in the text.
    """
    text = (str(err) if not isinstance(err, str) else err).lower()

    # Numeric GATT codes sometimes appear verbatim, e.g. "... (8)".
    if "(8)" in text or "code 8" in text or "status 8" in text:
        return "already_authorized"
    if (
        "(5)" in text or "code 5" in text or "status 5" in text
        or "(15)" in text or "(143)" in text
    ):
        return "encryption_needed"

    # Authorization look-alikes are checked BEFORE the broad encryption markers
    # so "insufficient authorization" isn't swallowed by a generic match.
    if any(m in text for m in _ALREADY_AUTHORIZED_MARKERS):
        return "already_authorized"
    if any(m in text for m in _ENCRYPTION_NEEDED_MARKERS):
        return "encryption_needed"
    return "fatal"


# Object path we export our own agent under on the D-Bus. BlueZ treats this as
# a freely-definable local name (see the org.bluez.Agent1 doc: "Object path:
# freely definable"), so it is purely a label for OUR object — namespaced under
# the integration so it is unambiguously this project's own agent.
_AGENT_PATH = "/org/homeassistant/wallbox_ble/agent"


async def _register_pairing_agent() -> object | None:
    """Register a NoInputNoOutput BlueZ pairing agent that auto-accepts.

    Implemented against the BlueZ D-Bus agent API (doc/org.bluez.AgentManager1
    RegisterAgent + doc/org.bluez.Agent1) and the dbus-fast ServiceInterface /
    @method export API. The charger pairs with the Just-Works model (no display
    or keypad on either end), so the agent advertises the `NoInputNoOutput`
    capability and answers the authorization/confirmation callbacks BlueZ makes
    during that flow by returning without error, which tells bluetoothd to
    accept the bond. This is what lets a first-time bond complete with no user
    interaction; see docs/ble-protocol.md.

    Returns the dbus message bus (keep it alive for the connection, disconnect
    on cleanup), or None if the agent could not be registered.
    """
    try:
        from dbus_fast.aio import MessageBus
        from dbus_fast.constants import BusType
        from dbus_fast.service import ServiceInterface, method
    except ImportError as err:
        _LOGGER.warning("dbus_fast unavailable, cannot register pairing agent: %s", err)
        return None

    class _AgentInterface(ServiceInterface):
        """org.bluez.Agent1 implementation for Just-Works auto-accept.

        Only the callbacks a NoInputNoOutput agent can receive are handled. Each
        returns normally, which BlueZ reads as "accepted"; raising instead would
        reject the pairing. The parameter type annotations ("o" object path,
        "s" string, "u" uint32) are the D-Bus signatures dbus-fast uses to
        marshal each method, taken from the Agent1 API definition.
        """

        def __init__(self) -> None:
            super().__init__("org.bluez.Agent1")

        @method()
        def Release(self):  # noqa: N802
            # bluetoothd calls this when it drops our agent; nothing to clean up.
            return

        @method()
        def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: F821, N802
            # Just-Works has no real passkey to compare; returning accepts it.
            return

        @method()
        def RequestAuthorization(self, device: "o"):  # noqa: F821, N802
            # Authorize the incoming Just-Works pairing attempt.
            return

        @method()
        def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: F821, N802
            # Authorize the service/connection request.
            return

        @method()
        def Cancel(self):  # noqa: N802
            # bluetoothd aborted the request before a reply; nothing to do.
            return

    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
        bus.export(_AGENT_PATH, _AgentInterface())
        introspection = await bus.introspect("org.bluez", "/org/bluez")
        obj = bus.get_proxy_object("org.bluez", "/org/bluez", introspection)
        agent_manager = obj.get_interface("org.bluez.AgentManager1")
        await agent_manager.call_register_agent(_AGENT_PATH, "NoInputNoOutput")
        _LOGGER.debug("Wallbox BLE pairing agent registered at %s", _AGENT_PATH)
        return bus
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not register BLE pairing agent: %s", err)
        return None


class WallboxBleClient:
    """Owns a persistent BLE connection and serialises BAPI round-trips.

    The link is maintained by a background task created with `start()` and
    stopped with `stop()`; callers only ever check `ready` and call `request()`
    / `request_many()`.
    """

    def __init__(self, hass: HomeAssistant, address: str) -> None:
        self._hass = hass
        self._address = address
        self._client: BleakClientWithServiceCache | None = None
        self._agent_bus: object | None = None
        self._parser = bapi.ResponseParser()
        self._rx_queue: asyncio.Queue[bytes] = asyncio.Queue()
        # Serialises every GATT round-trip (polls AND on-demand commands) so
        # they never overlap — overlapping ops cause org.bluez "In Progress".
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        # Set by the disconnected_callback / an op-timeout to wake the run loop.
        self._disconnected = asyncio.Event()
        # Set the moment the link is fully established (paired + notifying),
        # cleared on any drop. Lets a user-initiated write briefly await
        # readiness (wait_ready) instead of failing fast like a poll does.
        self._ready_event = asyncio.Event()
        self._stop = False
        self._next_id = 1
        # True only after pair + start_notify succeed; cleared on any drop. This
        # guards the zombie window where is_connected is still True but notify
        # is dead.
        self._connected = False
        # Tracks the unavailable<->available transition for single-shot logging.
        self._was_unavailable = False

    @property
    def ready(self) -> bool:
        """True when a fully-established link (connected + paired + notifying)
        is live. `is_connected` alone lies on a zombie link, so we also require
        `_connected`, which the disconnected_callback clears immediately."""
        return (
            self._client is not None
            and self._client.is_connected
            and self._connected
        )

    def start(self) -> None:
        """Start the persistent connection task (idempotent)."""
        if self._task is not None and not self._task.done():
            return
        self._stop = False
        if hasattr(self._hass, "async_create_background_task"):
            self._task = self._hass.async_create_background_task(
                self._run(), name=f"wallbox_ble {self._address}"
            )
        else:  # pragma: no cover - older HA cores
            self._task = asyncio.create_task(
                self._run(), name=f"wallbox_ble {self._address}"
            )

    async def stop(self) -> None:
        """Stop the persistent task and tear the link down."""
        self._stop = True
        self._disconnected.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        await self._teardown()

    async def _run(self) -> None:
        """Persistent connect/maintain/reconnect loop.

        Each iteration brings the link fully up, blocks until it drops, tears it
        down cleanly, then sleeps the current back-off before reconnecting.
        """
        backoff = BACKOFF_FLOOR
        while not self._stop:
            try:
                device = bluetooth.async_ble_device_from_address(
                    self._hass, self._address.upper(), connectable=True
                )
                if device is None:
                    # Not advertising right now (or the phone app holds the one
                    # BLE slot). Not fatal — back off and try again.
                    _LOGGER.debug(
                        "Wallbox %s not seen by HA Bluetooth; retrying in %.0fs",
                        self._address, backoff,
                    )
                    raise _NotReady()

                _LOGGER.debug("Connecting to Wallbox %s", self._address)
                self._disconnected.clear()
                self._client = await establish_connection(
                    BleakClientWithServiceCache,
                    device,
                    self._address,
                    disconnected_callback=self._on_disconnect,
                    use_services_cache=True,
                )
                _LOGGER.debug("Connected to %s", self._address)

                # Bring the link fully up — encrypt + subscribe — under a single
                # ~15s bring-up watchdog. This is a whole-bringup bound,
                # deliberately NOT the tiny per-op timeout (see
                # docs/ble-protocol.md).
                await asyncio.wait_for(
                    self._bring_up_link(), CONNECT_WATCHDOG
                )

                self._connected = True
                self._ready_event.set()
                backoff = BACKOFF_FLOOR
                if self._was_unavailable:
                    _LOGGER.info("Wallbox %s BLE link re-established", self._address)
                    self._was_unavailable = False

                # Block until the link drops (disconnected_callback or an
                # op-timeout sets the event). The `finally` tears down.
                await self._disconnected.wait()
                _LOGGER.debug("Wallbox %s link dropped; recycling", self._address)
            except _NotReady:
                pass
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Wallbox %s connect cycle failed: %s", self._address, err)
            finally:
                self._connected = False
                await self._teardown()

            if self._stop:
                break

            if not self._was_unavailable:
                # Single transition-to-unavailable warning, NOT one per retry.
                _LOGGER.warning(
                    "Wallbox %s BLE link unavailable; reconnecting with back-off",
                    self._address,
                )
                self._was_unavailable = True

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_CAP)

    async def _bring_up_link(self) -> None:
        """Encrypt the link via on-demand bonding, then subscribe.

        This replaces an old "force a re-pair on every connect" path, which was
        the PRIMARY cause of `Insufficient authorization (8)` + mid-handshake
        drops. The model (see docs/ble-protocol.md):

          * NEVER re-bond a device that is already bonded. Check bond state and
            only arm the bond flow when the device is NOT bonded.
          * Establish encryption ON DEMAND by READING a protected characteristic
            (the "mode" char). On BlueZ, reading an encryption-protected
            characteristic makes the stack negotiate security on demand — which
            is exactly what we want.
          * A bare createBond()/bluetooth_device_pair() is only ever the
            RECOVERY step after an encryption/authentication failure, and only
            when not already bonded.

        Sequence here (bond-state -> mode-read -> fallback -> subscribe):
          1. Read bond state. Non-fatal if unreadable -> 'unknown'.
          2. Read the mode char. Success => link is encrypted/usable; do NOT
             pair. Proceed to subscribe.
          3. On a read failure, classify it:
               - 'already_authorized' (GATT 8 etc.): the bond exists, stop
                 trying to re-pair -> treat as NON-FATAL, continue to subscribe.
               - 'encryption_needed' AND not already bonded/unknown: do ONE
                 best-effort fallback bond, then retry the read once.
               - 'fatal' (or still failing after fallback): raise so the connect
                 cycle recycles the link.
             The key invariant: NEVER force a re-pair on an already-bonded
             device.
          4. start_notify on TX (we only need TX for BAPI responses). No MTU
             request (the charger stays at the default 23-byte MTU).
        """
        # 1. Bond state. Maps Android BOND_BONDED to BlueZ Device1.Paired.
        #    None => couldn't read it; treat as 'unknown'.
        bonded = await self._is_bonded()
        _LOGGER.debug(
            "Wallbox %s bond state: %s", self._address,
            {True: "bonded", False: "not bonded", None: "unknown"}[bonded],
        )

        # The NoInputNoOutput agent assists a FIRST (Just-Works) bond. We keep
        # it registered for the cycle so a fallback bond can auto-accept, but it
        # is NOT what drives encryption any more — the mode-char read is.
        if self._agent_bus is None:
            self._agent_bus = await _register_pairing_agent()

        # 2. Primary encryption trigger: read the protected mode char (P2).
        ok, err = await self._try_read_mode_char()
        if not ok:
            verdict = classify_mode_read_error(err) if err is not None else "fatal"
            _LOGGER.debug(
                "Mode-char read failed (%s -> %s)", err, verdict,
            )

            if verdict == "already_authorized":
                # GATT 8 / "already exists" / AuthenticationFailed. On an
                # existing bond these mean "the bond is already there" — NOT a
                # reason to re-pair. Reads/writes work on the existing bond, so
                # continue to subscribe. This is the exact case that used to
                # trigger the disconnect storm.
                _LOGGER.debug(
                    "Treating authorization result as already-bonded; continuing "
                    "without re-pair",
                )
            elif verdict == "encryption_needed" and bonded is not True:
                # Not (known) bonded and the link needs encryption: do ONE
                # best-effort fallback bond, then retry the read once. This
                # mirrors the app using createBond() only as recovery, and only
                # when not already bonded.
                _LOGGER.debug("Link needs encryption and not bonded; fallback bond")
                await self._fallback_bond()
                ok2, err2 = await self._try_read_mode_char()
                if not ok2:
                    verdict2 = (
                        classify_mode_read_error(err2) if err2 is not None else "fatal"
                    )
                    if verdict2 == "already_authorized":
                        _LOGGER.debug(
                            "Post-bond read reports already-authorized; continuing",
                        )
                    else:
                        # Still not usable after a single fallback bond — recycle.
                        raise RuntimeError(
                            f"Mode-char read failed after fallback bond: {err2}"
                        )
            elif verdict == "encryption_needed" and bonded is True:
                # Already bonded but the read reported an encryption error. Do
                # NOT force a re-pair (the invariant). The bond should already
                # provide encryption; a transient error here is best handled by
                # letting the op layer retry. Continue to subscribe and let the
                # first real read/write confirm liveness.
                _LOGGER.debug(
                    "Already bonded; encryption error on read treated as "
                    "transient, continuing without re-pair",
                )
            else:
                # 'fatal' — unknown/disconnect error; recycle the link.
                raise RuntimeError(f"Mode-char read failed (fatal): {err}")

        # 4. Subscribe to TX notify. No MTU request (BGX uses default 23).
        await self._client.start_notify(UART_TX_CHAR_UUID, self._on_notify)
        _LOGGER.debug("Subscribed to Wallbox TX characteristic")

    async def _is_bonded(self) -> bool | None:
        """Return True/False if BlueZ bond state is readable, else None.

        Maps Android BOND_BONDED -> BlueZ org.bluez Device1.Paired (inferred).
        Best-effort: inspects the bleak BlueZ
        backend's cached device properties; any failure degrades to None
        ('unknown') rather than crashing the background task.
        """
        try:
            backend = getattr(self._client, "_backend", None)
            if backend is None:
                return None
            # BlueZ backend caches the org.bluez Device1 properties here.
            info = getattr(backend, "_device_info", None)
            if isinstance(info, dict):
                # Keys are org.bluez property names. "Paired" is BlueZ's bond
                # flag (there is no separate "Bonded" in the Device1 iface).
                for key in ("Paired", "Bonded"):
                    if key in info:
                        return bool(info[key])
            # Fall back to any 'details' mapping some backends expose.
            details = getattr(self._client, "details", None)
            if isinstance(details, dict):
                props = details.get("props") if "props" in details else details
                if isinstance(props, dict):
                    for key in ("Paired", "Bonded"):
                        if key in props:
                            return bool(props[key])
            return None
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not read bond state: %s", err)
            return None

    async def _try_read_mode_char(self) -> tuple[bool, BaseException | None]:
        """Read the protected mode char to trigger on-demand encryption (P2).

        Returns (True, None) on success (link is encrypted/usable), or
        (False, exception) on failure, leaving classification to the caller.
        Never raises — a failure is returned as data so the state machine stays
        in one place.
        """
        try:
            value = await asyncio.wait_for(
                self._client.read_gatt_char(MODE_CHAR_UUID), OP_TIMEOUT
            )
            _LOGGER.debug(
                "Mode-char read OK (%d bytes) — link encrypted", len(value or b"")
            )
            return True, None
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            return False, err

    async def _fallback_bond(self) -> None:
        """Single best-effort OS bond, used ONLY as recovery (P1/P2 fallback).

        This is the OLD force-pair path, now reached only when the link needs
        encryption AND the device is not already bonded. It is best-effort and
        non-fatal: a failure here is logged but does not tear the link down, so
        the subsequent mode-char retry (or a real read) decides usability. We
        NEVER call this on an already-bonded device.
        """
        # 1. Try bleak's high-level pair(). Returns None on success on HA BlueZ.
        try:
            await self._client.pair()
            _LOGGER.debug("Fallback bond via high-level pair()")
            return
        except NotImplementedError:
            _LOGGER.debug("pair() not implemented; using backend bond path")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("pair() high-level error: %s; trying backend path", err)

        # 2. Fall back to the BlueZ backend's own bond call. On some HA/bleak
        #    versions the high-level pair() raises NotImplementedError, so we
        #    reach the BlueZ backend's device-pair method directly. This walks
        #    bleak's private backend via getattr (each hop guarded) so a bleak
        #    internals change degrades to a logged no-op instead of an
        #    AttributeError crash on the background task.
        try:
            backend = getattr(self._client, "_backend", None)
            bluez_dbus = getattr(backend, "_client", None) if backend else None
            address_int = getattr(backend, "_address_as_int", None) if backend else None
            bond = getattr(bluez_dbus, "bluetooth_device_pair", None)
            if bond is None or address_int is None:
                _LOGGER.debug(
                    "Backend bond path unavailable on this bleak version; "
                    "leaving encryption to the mode-char retry"
                )
                return
            await bond(address_int)
            _LOGGER.debug("Fallback bond via BlueZ backend device-pair")
        except Exception as err:  # noqa: BLE001
            # Non-fatal: the mode-char retry / first real read will decide.
            _LOGGER.debug(
                "Fallback bond attempt did not complete: %s (%s)",
                err, type(err).__name__,
            )

    def _on_disconnect(self, _client) -> None:
        """BlueZ drop callback — fire the event so `_run` recycles the link.

        Kept tiny and non-async: it runs from bleak's loop context.
        """
        self._connected = False
        self._ready_event.clear()
        self._disconnected.set()

    def _on_notify(self, _char, data: bytearray) -> None:
        """Queue an incoming notification chunk; reassembly happens in the
        reader so a half-JSON fragment on a zombie link is discarded when the
        queue is drained per request."""
        self._rx_queue.put_nowait(bytes(data))

    async def request(self, met: str, par: str = "null") -> dict | None:
        """Send a BAPI command and return the parsed payload (the "r" value),
        or None. Fails fast (returns None, never raises) when the link is not
        ready, so callers never hammer a dead characteristic."""
        if not self.ready:
            return None
        async with self._lock:
            return await self._request_locked(met, par)

    async def request_many(self, calls: list[tuple[str, str]]) -> list[dict | None]:
        """Run several round-trips under a SINGLE lock hold so they cannot be
        interleaved by a poll (used for the atomic w_sch write + r_schs
        read-back). Returns None for every call if the link is not ready."""
        if not self.ready:
            return [None for _ in calls]
        results: list[dict | None] = []
        async with self._lock:
            for met, par in calls:
                results.append(await self._request_locked(met, par))
        return results

    async def wait_ready(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for the link to become ready.

        Unlike `request`/`request_many` (which fail fast so polls never block),
        this gives a USER-initiated write a short grace period to ride out the
        normal drop/reconnect churn on this charger. Returns True as soon as the
        link is ready, or False on timeout. Never raises; never blocks the loop
        (it awaits an asyncio.Event with a wait_for deadline).
        """
        if self.ready:
            return True
        try:
            # The event is set the instant the run loop finishes establishing
            # the link. Loop because the event may be re-cleared by a drop
            # between the wake and our `ready` re-check.
            deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._ready_event.wait(), remaining)
                if self.ready:
                    return True
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("wait_ready error: %s", err)
        return self.ready

    async def _attempt_once(
        self, met: str, par: str, attempt: int
    ) -> tuple[dict | None, bool, Exception | None]:
        """A single write+read round-trip. Caller MUST hold `self._lock`.

        Returns (parsed_or_None, timed_out, error). `timed_out` is True when the
        op hit OP_TIMEOUT (a retry candidate); `error` is set for a non-timeout
        failure (a genuine broken link). Each attempt builds a FRESH request id
        and drains stale RX so a retry never matches a previous reply.
        """
        req_id = self._next_id
        self._next_id += 1

        payload = bapi.build_command(met, par, req_id)
        framed = bapi.frame(payload)

        # Drop any stale/partial RX so this request only matches its own reply.
        while not self._rx_queue.empty():
            self._rx_queue.get_nowait()
        self._parser.reset()

        _LOGGER.debug("TX %s (id=%s, attempt=%d): %s", met, req_id, attempt, framed.hex())
        try:
            await asyncio.wait_for(
                self._client.write_gatt_char(
                    UART_RX_CHAR_UUID, framed, response=True
                ),
                OP_TIMEOUT,
            )
            parsed = await asyncio.wait_for(
                self._collect_response(req_id), OP_TIMEOUT
            )
            return parsed, False, None
        except asyncio.TimeoutError:
            # A slow reply, NOT necessarily a dead link — signal the caller to
            # retry before declaring the link a zombie (P3).
            return None, True, None
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            return None, False, err

    async def _request_locked(self, met: str, par: str) -> dict | None:
        """One write/read round-trip. Caller MUST hold `self._lock`.

        Decouple "slow reply" from "dead link": on a per-op TIMEOUT we RETRY the
        op once (OP_RETRIES) before declaring the link a zombie — one slow reply
        must NOT single-handedly tear the link down (and, with an old force-pair
        path, force a re-pair). Only after the retry also times out do we fire
        `_disconnected` to recycle the link. See docs/ble-protocol.md.
        """
        parsed: dict | None = None
        for attempt in range(OP_RETRIES + 1):
            parsed, timed_out, err = await self._attempt_once(met, par, attempt)
            if err is not None:
                # A non-timeout error (write failed, disconnected mid-op) is a
                # genuine dead/broken link — recycle immediately, no retry.
                _LOGGER.debug("Error on %s: %s; forcing reconnect", met, err)
                self._connected = False
                self._ready_event.clear()
                self._disconnected.set()
                return None
            if not timed_out:
                break
            # Timed out. Retry once more before giving up (unless out of tries).
            if attempt < OP_RETRIES:
                _LOGGER.debug(
                    "Timeout on %s (attempt %d/%d); retrying before reconnect",
                    met, attempt + 1, OP_RETRIES + 1,
                )
                continue
            # Retry budget exhausted: now it's a zombie link. Force reconnect.
            _LOGGER.debug(
                "Timeout on %s after %d attempts; forcing reconnect",
                met, OP_RETRIES + 1,
            )
            self._connected = False
            self._ready_event.clear()
            self._disconnected.set()
            return None

        if parsed is None:
            return None
        # `_attempt_once` -> `_collect_response` already matched the reply to the
        # request id, so `parsed` is the right response. The payload is under
        # "r"; return the whole parsed dict if "r" is absent.
        _LOGGER.debug("RX %s: %s", met, parsed)
        return parsed.get("r", parsed)

    async def _collect_response(self, req_id: int) -> dict | None:
        """Read notify chunks off the queue until a complete, id-matched
        response reassembles. Returns the parsed dict (or None on parse fail)."""
        while True:
            data = await self._rx_queue.get()
            complete = self._parser.feed(data)
            if complete is None:
                continue
            parsed = bapi.ResponseParser.parse(complete)
            if parsed is None:
                # Garbage frame — keep reading for the real reply.
                self._parser.reset()
                continue
            if parsed.get("id") in (req_id, None):
                return parsed
            # A stale reply for a different id; keep waiting for ours.
            self._parser.reset()

    async def _teardown(self) -> None:
        """Tear the link down cleanly (idempotent)."""
        if self._client is not None:
            if self._client.is_connected:
                with contextlib.suppress(Exception):
                    await self._client.stop_notify(UART_TX_CHAR_UUID)
                with contextlib.suppress(Exception):
                    await self._client.disconnect()
            self._client = None
        self._connected = False
        self._ready_event.clear()
        if self._agent_bus is not None:
            with contextlib.suppress(Exception):
                self._agent_bus.disconnect()
            self._agent_bus = None


class _NotReady(Exception):
    """Internal: the device is not currently resolvable; back off and retry."""
