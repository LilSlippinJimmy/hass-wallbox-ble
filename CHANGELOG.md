# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
