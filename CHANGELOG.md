# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.4]

### Added
- **Schedule Days** select per slot — choose which days a schedule runs (Every
  day, Weekdays, Weekends, or a single day) instead of always defaulting to
  every day. Arbitrary combinations remain available via the
  `wallbox_ble.set_schedule` service's `days` bitmask.

### Changed
- Setting a schedule **start** time on an empty slot now defaults the **stop**
  to start + 1 minute (and setting stop defaults start to stop − 1 minute),
  instead of a fixed 23:41.

## [0.2.3]

### Added
- **OCPP Status** sensor — live OCPP backend connection state (Connected, Not
  activated, Connection error, Connection rejected, …) from `r_dat.ocpp` /
  `r_sta.ocpp_status`. Complements the static **OCPP Enabled** binary sensor.

### Docs
- README: guidance to remove the charger from your phone's Bluetooth list so
  the Wallbox app falls back to cloud and stops contending for the single BLE
  link.

## [0.2.2]

### Fixed
- Removed the `categories` manifest key: it is only valid for core integrations
  and was failing hassfest validation on this custom integration.
- Removed the unused `render_readme` key from `hacs.json`.

## [0.2.1]

### Added
- In-repo brand assets (`custom_components/wallbox_ble/brand/`) so the HACS
  brands validation passes.
- (Reverted in 0.2.2) Manifest `categories` key.

## [0.2.0]

### Added
- Live **session** sensors: energy, power, duration, grid energy, green energy.
- **Last session energy** sensor.
- **Lock Status** sensor (read-only).
- **Plugged In** and **OCPP Enabled** binary sensors.
- **Max Charging Current** number entity (write via `w_mxI`, read back).
- **Reboot** button (`rebot`).
- **LED halo standby** controls: **Halo Standby Light** switch and **Halo
  Brightness** number (`g_halocfg`/`s_halocfg`, read-modify-write).
- **Timezone** diagnostic sensor (`g_tzn`).
- **Battery** sensor — vehicle state-of-charge (%) from `r_lse.soc`, shown only
  when the EV reports SoC to the charger.
- Diagnostic sensors: **Firmware Build**, **WLAN MAC**, **Serial Number**
  (read once at startup). Firmware Build is the internal build number reported
  over BLE (e.g. 181); the dotted app version (e.g. 6.7.43) is cloud-only.
- OCPP awareness: documented which controls the OCPP backend owns; charging and
  lock are kept read-only under OCPP.

### Changed
- **BREAKING:** the **Charging** control is now a read-only `binary_sensor`
  instead of a `switch`. Under OCPP you cannot reliably force a charge locally,
  so it only reflects state. Remove any automations that called
  `switch.turn_on/off` on the old charging switch.
- Static info (firmware, MAC, OCPP flag, serial) is read once at startup rather
  than every poll, to keep the BLE link light.

## [0.1.0] - Initial public release

### Added
- Local BLE control of Wallbox Pulsar / Pulsar Plus chargers (no cloud).
- Bluetooth auto-discovery and manual device add, with optional BLE PIN.
- Persistent background connection with event-driven drop detection and
  exponential back-off reconnect.
- Sensors: status, charge power, charging current, max available current,
  schedule count, and per-slot schedule details.
- Charging start/stop switch.
- Editable per-slot schedule start/stop time entities.
- Schedule delete via per-slot buttons and the `wallbox_ble.delete_schedule`
  service (emulated by rewrite-and-compact on firmware with no native delete).
- `wallbox_ble.set_schedule` and `wallbox_ble.probe` services.
- Configurable BLE poll interval via the options flow.
