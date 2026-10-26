"""Constants for the Wallbox BLE (local) integration."""

DOMAIN = "wallbox_ble"

CONF_ADDRESS = "address"
CONF_PIN = "pin"

# Wallbox custom GATT service advertised by the charger (from BLE scan).
WALLBOX_SERVICE_UUID = "331a36f5-2459-45ea-9d95-6142f0c4b307"

# Options.
CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN_INTERVAL = 15
MIN_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 120

# Services.
SERVICE_PROBE = "probe"
SERVICE_SET_SCHEDULE = "set_schedule"
SERVICE_DELETE_SCHEDULE = "delete_schedule"
