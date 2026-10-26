"""BLE client for a Wallbox Pulsar charger using Home Assistant's Bluetooth stack.

Speaks the BAPI protocol over the charger's custom UART-style GATT service.
The Pulsar Plus requires a BLE pair, which only succeeds when a
NoInputNoOutput BlueZ agent is registered to auto-accept the authorization
request (a bare client.pair() yields org.bluez.Error.AuthenticationFailed).

Characteristics / pairing approach verified against this charger and modelled
on the jagheterfredrik/wallbox-ble project (MIT).

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

# Short per-operation timeout on a single write/read round-trip. A longer wait
# (the old 8s) just blocks on a dead link; 3s is plenty for a live charger and
# turns a zombie link into a prompt reconnect.
OP_TIMEOUT = 3.0

# Exponential reconnect back-off bounds.
BACKOFF_FLOOR = 5.0
BACKOFF_CAP = 120.0


async def _register_pairing_agent() -> object | None:
    """Register a NoInputNoOutput BlueZ pairing agent that auto-accepts.

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
        def __init__(self) -> None:
            super().__init__("org.bluez.Agent1")

        @method()
        def RequestAuthorization(self, device: "o"):  # noqa: F821, N802
            return

        @method()
        def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: F821, N802
            return

        @method()
        def Cancel(self):  # noqa: N802
            return

    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
        bus.export("/wallbox/agent", _AgentInterface())
        introspection = await bus.introspect("org.bluez", "/org/bluez")
        obj = bus.get_proxy_object("org.bluez", "/org/bluez", introspection)
        agent_manager = obj.get_interface("org.bluez.AgentManager1")
        await agent_manager.call_register_agent("/wallbox/agent", "NoInputNoOutput")
        _LOGGER.debug("Wallbox BLE pairing agent registered")
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

    def __init__(
        self, hass: HomeAssistant, address: str, pin: str | None = None
    ) -> None:
        self._hass = hass
        self._address = address
        self._pin = pin
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

                # Establish SMP encryption BEFORE subscribing/writing. Reads
                # work on an unpaired link, but settings writes (w_sch / ...)
                # are rejected until the link is encrypted. Most chargers
                # without a BAPI PIN use "Just Works" pairing (no prompt), which
                # needs a NoInputNoOutput agent registered to auto-accept.
                self._agent_bus = await _register_pairing_agent()
                await self._pair()

                await self._client.start_notify(UART_TX_CHAR_UUID, self._on_notify)
                _LOGGER.debug("Subscribed to Wallbox TX characteristic")

                self._connected = True
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

    async def _pair(self) -> None:
        """Pair/encrypt the link, trying the HA-backend bonding path explicitly.

        bleak's high-level pair() often raises NotImplementedError on HA's
        BlueZ backend, so we fall back to the backend's bluetooth_device_pair,
        exactly as the jagheterfredrik/wallbox-ble reference does. Every step is
        logged (and NOT suppressed) so we can see what actually happened.
        """
        # 1. Try the high-level API. (Returns None on success on HA's backend.)
        try:
            await self._client.pair()
            _LOGGER.debug("Paired via high-level pair()")
            return
        except NotImplementedError:
            _LOGGER.debug("pair() not implemented on this backend; using backend path")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("pair() high-level error: %s; trying backend path", err)

        # 2. Fall back to the BlueZ backend bonding call.
        try:
            backend = self._client._backend  # noqa: SLF001
            await backend._client.bluetooth_device_pair(  # noqa: SLF001
                backend._address_as_int  # noqa: SLF001
            )
            _LOGGER.debug("Paired via backend bluetooth_device_pair()")
        except Exception as err:  # noqa: BLE001
            # Writes need an encrypted link; a genuine pair failure is worth a
            # warning since it will break schedule/switch writes.
            _LOGGER.warning("BLE pairing failed: %s (%s)", err, type(err).__name__)

    def _on_disconnect(self, _client) -> None:
        """BlueZ drop callback — fire the event so `_run` recycles the link.

        Kept tiny and non-async: it runs from bleak's loop context.
        """
        self._connected = False
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

    async def _request_locked(self, met: str, par: str) -> dict | None:
        """One write/read round-trip. Caller MUST hold `self._lock`."""
        req_id = self._next_id
        self._next_id += 1

        payload = bapi.build_command(met, par, req_id)
        framed = bapi.frame(payload)

        # Drop any stale/partial RX so this request only matches its own reply.
        while not self._rx_queue.empty():
            self._rx_queue.get_nowait()
        self._parser.reset()

        _LOGGER.debug("TX %s (id=%s): %s", met, req_id, framed.hex())
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
        except asyncio.TimeoutError:
            # A timeout is the zombie-link signal (is_connected lies True). Break
            # the link so the persistent loop recycles it instead of writing to
            # a dead characteristic forever. DEBUG only — the loop logs the
            # single transition-to-unavailable warning.
            _LOGGER.debug("Timeout on %s; forcing reconnect", met)
            self._connected = False
            self._disconnected.set()
            return None
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Error on %s: %s; forcing reconnect", met, err)
            self._connected = False
            self._disconnected.set()
            return None

        if parsed is None:
            return None
        # Only return the response matching our request id; the payload is under
        # "r". Return the whole parsed dict if "r" is absent.
        if parsed.get("id") not in (req_id, None):
            _LOGGER.debug("Ignoring mismatched response id %s", parsed.get("id"))
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
        if self._agent_bus is not None:
            with contextlib.suppress(Exception):
                self._agent_bus.disconnect()
            self._agent_bus = None


class _NotReady(Exception):
    """Internal: the device is not currently resolvable; back off and retry."""
