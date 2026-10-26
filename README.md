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
- **Sensors** — status, charge power, charging current, max available current,
  schedule count, and per-slot schedule details.
- **Charging switch** — start/stop charging.
- **Editable schedules** — per-slot start/stop time pickers.
- **Schedule delete** — per-slot delete buttons and a service (see
  [How schedule delete works](#how-schedule-delete-works)).
- **Configurable poll interval** via the integration's *Configure* dialog.

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

| Entity | Type | Description |
| --- | --- | --- |
| Status | sensor | Human-readable charger state (Ready, Charging, Connected, Locked, Paused, Error, …). Attributes expose the raw status/lock codes. |
| Charge Power | sensor | Instantaneous charging power (kW). |
| Charging Current | sensor | Charging current setpoint (A). |
| Max Available Current | sensor | Maximum available current (A). |
| Schedules | sensor | Number of schedules configured; all slots exposed as attributes. |
| Schedule 0–3 | sensor | Per-slot window `HH:MM-HH:MM` (or `none`), with full fields in attributes. |
| Charging | switch | Start/stop charging. |
| Schedule 0–3 Start / Stop | time | Editable per-slot start/stop times (shown in local time, stored on the charger as UTC). |
| Delete Schedule 0–3 | button | Delete that schedule slot (only available when the slot is in use). |

> "Locked" status means no car is plugged in.

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

This is an independent, community project. "Wallbox" and "Pulsar" are trademarks
of their respective owners. Use of this software is at your own risk; the authors
accept no liability for any damage to your charger, vehicle, or property.
