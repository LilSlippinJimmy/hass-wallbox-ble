# Wallbox BLE connection & encryption model

This document describes, in the integration's own words, how `wallbox_ble`
establishes and maintains its Bluetooth Low Energy link to a Wallbox Pulsar /
Pulsar Plus charger. It exists so the client code can stay lightly commented and
point here for the rationale, rather than carrying protocol detail inline.

## No application-layer PIN

The charger does **not** use an application-layer PIN or an auth handshake in
the BAPI protocol. Encryption and trust are handled entirely at the operating
system / Bluetooth stack level through **bonding**. There is nothing for a user
to type in: once the link is bonded, reads and writes are permitted.

Because of this, the integration asks for **no PIN** during setup. (Earlier
versions exposed a PIN field that was only stored and never sent to the charger;
it has been removed.)

## Bonding (Just-Works, NoInputNoOutput)

Neither the host nor the charger has a display or keypad for the pairing
exchange, so bonding uses the **Just-Works** association model. On BlueZ this
requires a pairing **agent** to be registered that auto-accepts the
authorization request. The integration registers a `NoInputNoOutput` agent for
the lifetime of a connection cycle so a first-time bond can complete without any
user interaction. A bare pair attempt without such an agent is rejected by
BlueZ with an authentication failure.

## On-demand encryption via a protected characteristic

Rather than forcing a bond on every connect, the integration establishes
encryption **on demand**:

- The charger exposes a protected "mode" characteristic that requires an
  encrypted link to read.
- Reading that characteristic prompts the Bluetooth stack to negotiate security
  (encrypt the link) at that moment.
- A **successful read** therefore means the link is encrypted and usable — and
  the integration does **not** then try to pair again.

This matters: forcing a re-pair on an already-bonded device is what produced a
recurring `Insufficient authorization (8)` drop and a reconnect storm. The
invariant the client enforces is: **never force a re-pair on a device that is
already bonded.**

### Classifying a failed read

If the protected-characteristic read fails, the error text from BlueZ/bleak is
classified into three outcomes (see `classify_mode_read_error` in `client.py`):

- **already_authorized** — e.g. GATT status 8, "already exists",
  authentication-failed look-alikes. On an existing bond these mean "the bond is
  already there"; they are non-fatal and the client continues to subscribe.
- **encryption_needed** — e.g. GATT status 5/15/143, "not paired". When the
  device is not already bonded, the client performs **one** best-effort fallback
  bond and retries the read once.
- **fatal** — anything else (disconnect, unrecognised error): the connect cycle
  recycles the link.

## Timeouts (why they are decoupled)

Two independent time bounds guard the link, and they are deliberately kept
separate:

- A **per-operation timeout** (`OP_TIMEOUT`, ~8s) bounds a single write/read
  round-trip. A slow reply is retried once (`OP_RETRIES`) before the link is
  treated as a zombie. A single slow reply must not, by itself, tear the link
  down — a too-tight per-op timeout turns ordinary slowness into a reconnect
  storm.
- A **bring-up watchdog** (`CONNECT_WATCHDOG`, ~15s) bounds the whole
  connect + encrypt + subscribe sequence, so a stuck bring-up recycles instead
  of hanging the background connection task.

Mixing these two concerns into one tight timeout was the original cause of
instability; keeping them separate is intentional.

## Persistent connection model

A single background task owns the BLE link for the lifetime of the integration.
Each cycle it resolves a fresh device from Home Assistant's Bluetooth stack,
connects, registers the pairing agent, brings the link up (encrypt + subscribe
to the notify characteristic), then blocks until the link drops. A disconnect
callback — or a per-op timeout signalling a half-dead ("zombie") link where
`is_connected` still reports true — wakes the loop, which tears the link down
cleanly and reconnects with exponential back-off (floor 5s, cap 120s). The
back-off resets only after a successful connect and first successful read.

Requests fail fast: when the link is not ready, `request()` returns `None`
immediately instead of blocking, so polls and on-demand commands never hammer a
dead characteristic while the background task reconnects.
