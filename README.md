# Wallbox BLE (Local) — Home Assistant integration

Local, cloud-free control of a **Wallbox Pulsar / Pulsar Plus** EV charger over
Bluetooth Low Energy, straight from Home Assistant — no Wallbox account, no
cloud round-trips, no MQTT broker, and no extra ESP32 gateway hardware.

Home Assistant talks to the charger directly over BLE (via a built-in or USB
Bluetooth adapter, or an [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html))
using the charger's native "BAPI" protocol.

> **Status: early release (0.1.x).** Reverse-engineered and tested against a
> Wallbox Pulsar Plus. It works well for the author, but it is not an official
> Wallbox product. Behaviour may vary across firmware versions and Wallbox
> models. Use at your own risk — see [Limitations](#limitations).

---

## Features

- **Fully local** — direct BLE, no cloud, no Wallbox account required.
- **Auto-discovery** — the charger is detected by Home Assistant's Bluetooth
  integration and offered as a discovered device. Manual add is also supported.
- **Resilient connection** — a persistent background connection with
  event-driven drop detection and exponential back-off, so a dropped link
  recovers on its own instead of going permanently unavailable.
- **Status & power sensors** — charger state, charge power, charging/available
  current.
- **Session sensors** — live session energy, power, duration, grid/green
  energy, plus last-session energy.
- **Charging / Plugged-in binary sensors** — read-only state (see
  [OCPP](#ocpp-controlled-chargers)).
- **Max charging current** — settable via a number entity.
- **Editable schedules** — per-slot start/stop time pickers.
- **Schedule delete** — per-slot delete buttons and a service (see
  [How schedule delete works](#how-schedule-delete-works)).
- **Reboot button** — restart the charger over BLE.
- **LED halo control** — toggle the standby ring light and set its brightness.
- **Diagnostics** — firmware build, MAC, serial, timezone, and OCPP-enabled status.
- **OCPP-aware** — detects OCPP control and keeps charging/lock read-only when
  the backend owns them (see [OCPP-controlled chargers](#ocpp-controlled-chargers)).
- **Configurable poll interval** via the integration's *Configure* dialog (see
  [Polling & update timing](#polling--update-timing)).

---

## Requirements

- Home Assistant with the **Bluetooth** integration set up and a working
  adapter that can reach the charger. This can be:
  - a local/USB Bluetooth adapter on the Home Assistant host, or
  - an [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html)
    placed near the charger.
- The charger within reliable BLE range. Wallbox chargers allow **only one BLE
  connection at a time**, so the Wallbox phone app will contend for the link —
  keep the app closed (or Bluetooth off on the phone) for best reliability.
- A Wallbox **Pulsar** or **Pulsar Plus**. Other models may work but are
  untested.

---

## Installation

### Option A — HACS (custom repository)

> This integration is not yet in the default HACS list. You can add it as a
> custom repository today; official HACS inclusion is planned.

1. In Home Assistant, open **HACS → Integrations**.
2. Click the three-dot menu (top right) → **Custom repositories**.
3. Add the repository URL:
   `https://github.com/LilSlippinJimmy/hass-wallbox-ble`
   and set the category to **Integration**.
4. Find **Wallbox BLE (Local)** in HACS and click **Download**.
5. **Restart Home Assistant.**

### Option B — Manual

1. Download this repository (Code → Download ZIP, or `git clone`).
2. Copy the `custom_components/wallbox_ble` folder into your Home Assistant
   configuration directory so the result is:
   ```
   <config>/custom_components/wallbox_ble/
   ```
   (`<config>` is the folder containing your `configuration.yaml`.)
3. **Restart Home Assistant.**

---

## Setup

After installing and restarting:

- **If auto-discovered:** go to **Settings → Devices & Services**. The charger
  should appear as a discovered **Wallbox BLE (Local)** device — click
  **Configure** / **Add**.
- **To add manually:** **Settings → Devices & Services → Add Integration →
  Wallbox BLE (Local)**, then pick your charger from the list of Wallbox devices
  currently visible to Home Assistant's Bluetooth.

If your charger has a **BLE PIN** set in the Wallbox app, enter it when prompted.
Most chargers without a PIN pair automatically ("Just Works").

### Options

Open the integration entry and click **Configure** to set the **poll interval**
(how often the charger is read over BLE). Default is 15 seconds; range 10–120.
A longer interval is gentler on a marginal BLE link.

---

## Entities

The **Refresh** column shows how each entity's value is kept up to date:

- **poll** — re-read from the charger every poll cycle (the interval you set in
  the integration's *Configure* dialog; default 15 s). See
  [Polling & update timing](#polling--update-timing).
- **static** — read **once** when the integration loads, then cached. These
  values don't change at runtime; reload the integration (or restart Home
  Assistant) to refresh them (e.g. after a firmware update).
- **on action** — updates immediately after you use the related control.

| Entity | Type | Refresh | Description |
| --- | --- | --- | --- |
| Status | sensor | poll | Human-readable charger state (Ready, Charging, Connected, Locked, Paused, Error, …). Attributes expose the raw status/lock codes. |
| Charge Power | sensor | poll | Instantaneous charging power (kW), from `r_dat`. |
| Charging Current | sensor | poll | Charging current setpoint (A). |
| Max Available Current | sensor | poll | Maximum available current the charger will allow (A). |
| Session Energy | sensor | poll | Energy delivered in the current charging session (kWh). |
| Session Power | sensor | poll | Live charging power for the session (kW). |
| Session Duration | sensor | poll | Elapsed charging time this session (s). |
| Session Grid Energy | sensor | poll | Grid energy used this session (kWh). |
| Session Green Energy | sensor | poll | Green/solar energy used this session (kWh). |
| Last Session Energy | sensor | poll | Energy of the last completed session (Wh). |
| Lock Status | sensor | poll | `locked` / `unlocked`, from `r_lck` (read-only — see [OCPP](#ocpp-controlled-chargers)). |
| Battery | sensor | poll | Vehicle state-of-charge (%). Only populated if your EV reports SoC to the charger; otherwise unavailable. |
| Schedules | sensor | poll | Number of schedules configured; all slots exposed as attributes. |
| Schedule 0–3 | sensor | poll | Per-slot window `HH:MM-HH:MM` (or `none`), with full fields in attributes. |
| Charging | binary_sensor | poll | On when the charger is actively charging (read-only — see [OCPP](#ocpp-controlled-chargers)). |
| Plugged In | binary_sensor | poll | On when a vehicle is connected (status is not Locked). |
| OCPP Enabled | binary_sensor | static | On when the charger is managed by an OCPP backend. |
| Firmware Build | sensor | static | Charger firmware build number (diagnostic). Over BLE the charger reports the internal build number (e.g. `181`), not the dotted version (e.g. `6.7.43`) the app shows — that dotted version is cloud-only. |
| WLAN MAC | sensor | static | Charger Wi‑Fi MAC (diagnostic, disabled by default). |
| Serial Number | sensor | static | Charger serial (diagnostic, disabled by default). |
| Timezone | sensor | static | Charger timezone, e.g. `Europe/London` (diagnostic). |
| Halo Standby Light | switch | on action | Turn the LED ring standby light on/off. |
| Halo Brightness | number | on action | LED ring standby brightness (0–100%). |
| Max Charging Current | number | on action | Set the max charging current (A). Read back after writing (see [OCPP](#ocpp-controlled-chargers)). |
| Schedule 0–3 Start / Stop | time | poll / on action | Editable per-slot start/stop times (shown in local time, stored on the charger as UTC). |
| Delete Schedule 0–3 | button | — | Delete that schedule slot (only available when the slot is in use). |
| Reboot | button | — | Reboot the charger. |

> "Locked" status means no car is plugged in.

---

## Polling & update timing

The integration holds **one persistent BLE connection** and reads the charger
on a fixed interval. You can change this interval in the integration's
**Configure** dialog (**Settings → Devices & Services → Wallbox BLE (Local) →
Configure**): the **poll interval**, default **15 seconds**, range **10–120 s**.

**What the poll interval affects.** Every entity marked **poll** in the table
above is re-read each cycle, so the poll interval is how quickly those values
update in Home Assistant. A shorter interval = fresher data but more BLE
traffic; a longer interval = gentler on a weak/contended link. If your charger
is far from the adapter (low RSSI) or the Wallbox app is often connected,
prefer a longer interval.

**What the poll interval does NOT affect:**

- **static** entities (OCPP Enabled, Firmware, MAC, Serial) are read once at
  startup and never re-polled. Changing the poll interval does nothing to them.
  Reload the integration or restart Home Assistant to refresh them.
- **on action / button** entities (Max Charging Current, schedule edits/delete,
  Reboot) act when you use them and read back immediately — they don't wait for
  the next poll.

Why static values are read once: firmware version, MAC, serial and the OCPP
flag don't change while the charger runs, so re-reading them every cycle would
add Bluetooth traffic for no benefit. Reading them a single time keeps each poll
light — important given the charger allows only one BLE connection at a time.

---

## OCPP-controlled chargers

If your charger is managed by an **OCPP** backend (for example Octopus
Intelligent, Ohme, or another smart-charging provider), charging is controlled
remotely by that backend. The **OCPP Enabled** binary sensor tells you whether
this is the case.

When OCPP is enabled:

- **Charging state is read-only.** You can't reliably force a charge on/off
  locally — the backend decides. That's why "Charging" is a sensor, not a
  switch.
- **Lock is read-only.** Lock status is shown, but local lock/unlock is not
  exposed because the firmware rejects or overrides it under OCPP.
- **Max Charging Current can be overridden.** You can set it, and it's written
  and read back, but the OCPP backend may change it again during a session.
- **Reboot still works** — it isn't gated by OCPP.

---

## Services

### `wallbox_ble.set_schedule`
Write/replace a single schedule slot.

| Field | Required | Description |
| --- | --- | --- |
| `start` | yes | Start time, `HHMM` (e.g. `0200`). |
| `stop` | yes | Stop time, `HHMM` (e.g. `0230`). |
| `sid` | no (default 0) | Schedule slot 0–4. |
| `days` | no (default 127) | Day bitmask, 127 = every day. |
| `max_current` | no | Max charging current (A). |
| `timezone` | no (default `local`) | `local` converts local wall-clock to the UTC the charger stores; `utc` sends as-is. |

```yaml
action: wallbox_ble.set_schedule
data:
  start: "0200"
  stop: "0430"
  sid: 0
  timezone: local
```

### `wallbox_ble.delete_schedule`
Delete a schedule slot (see [how it works](#how-schedule-delete-works)).

```yaml
action: wallbox_ble.delete_schedule
data:
  sid: 1
```

### `wallbox_ble.probe`
Diagnostic. With no fields, reads a default safe set (status, schedules, serial,
lock) and logs the results. Optionally call one specific BAPI method with a raw
JSON parameter for troubleshooting.

> ⚠️ A custom `method` can change charger state. Only use it if you know what the
> method does.

```yaml
action: wallbox_ble.probe
data:
  method: r_schs
```

---

## How schedule delete works

Some Wallbox firmware (including the Pulsar Plus this was developed against) has
**no native per-slot delete command** over BLE — the obvious candidates
(`clr_sch`, `s_sch`) are rejected by the charger. The only schedule write the
firmware accepts is `w_sch`, which writes one slot by position.

So "delete" is implemented the same way the official app achieves it on these
chargers: the integration reads the current schedules, drops the target slot,
and **rewrites the remaining schedules compacted down** into the lower slot
positions, clearing any freed trailing slot. The net effect is the slot is gone.

**Note:** because slots are compacted, deleting one schedule may renumber the
others (e.g. a schedule in slot 2 moves to slot 1). This matches how the Wallbox
app renumbers.

---

## Limitations

- **Reverse-engineered & unofficial.** Not affiliated with or endorsed by
  Wallbox. The BAPI protocol is undocumented; behaviour can differ by firmware
  and model.
- **Tested on Pulsar Plus.** Other models are untested. Some BAPI methods differ
  across models/firmware.
- **One BLE connection at a time.** The Wallbox phone app competes for the
  single BLE link. Transient contention is tolerated, but keeping the app closed
  improves reliability.
- **Marginal signal.** At weak RSSI, polling can be unreliable. Use a longer poll
  interval and/or an ESPHome Bluetooth proxy near the charger.
- **No native schedule delete on some firmware** — emulated via compaction (see
  above).
- **Not available over Bluetooth (so not provided):** some values the Wallbox
  app shows come from the Wallbox **cloud**, not the charger's BLE interface, so
  a local integration cannot read them:
  - **Energy price / tariff** — set in the app but stored in your Wallbox cloud
    account, not served over BLE.
  - **Added range** — derived from vehicle efficiency; not a charger value.
  - **Dotted software version** (e.g. `6.7.43`) — cloud-only; over BLE the
    charger reports only the firmware build number (see *Firmware Build*).

---

## Troubleshooting

- **Entities show "unavailable":** the link may be establishing or the phone app
  may be holding it. The integration reconnects automatically with back-off;
  closing the Wallbox app usually helps.
- **Writes/schedules don't apply:** confirm the charger is in BLE range and the
  app isn't connected. If your charger has a BLE PIN, make sure it was entered
  during setup.
- **Enable debug logging** to see what's happening over BLE:
  ```yaml
  logger:
    logs:
      custom_components.wallbox_ble: debug
  ```

---

## Credits

This integration stands on the work of others who reverse-engineered the Wallbox
BLE protocol:

- [`jagheterfredrik/wallbox-ble`](https://github.com/jagheterfredrik/wallbox-ble)
  — the BLE pairing approach (NoInputNoOutput BlueZ agent) and persistent
  connection pattern.
- [`botts7/esp32-wallbox`](https://github.com/botts7/esp32-wallbox) — the BAPI
  framing/checksum and method-name reference.

Content from these sources was adapted and re-implemented for Home Assistant's
Bluetooth stack.

## License

Released under the [MIT License](LICENSE).

## Disclaimer

This is an independent, community project, built with heavy use of AI-assisted
development and reverse engineering. "Wallbox" and "Pulsar" are trademarks of
their respective owners. Use of this software is at your own risk; the authors
accept no liability for any damage to your charger, vehicle, or property.
