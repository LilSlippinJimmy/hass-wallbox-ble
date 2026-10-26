"""Config flow for Wallbox BLE (local).

Supports two entry paths:
  - Bluetooth auto-discovery: the charger is detected by its advertised
    service UUID / name and offered as a discovered device.
  - Manual user add: pick from the Wallbox chargers currently visible to
    Home Assistant's Bluetooth.
In both cases an optional BLE PIN can be supplied (only needed if a PIN is
set in the Wallbox app).
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_PIN,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    WALLBOX_SERVICE_UUID,
)

_LOGGER = logging.getLogger(__name__)


def _is_wallbox(info: BluetoothServiceInfoBleak) -> bool:
    """Return True if a discovered BLE device looks like a Wallbox charger."""
    if WALLBOX_SERVICE_UUID.lower() in (u.lower() for u in info.service_uuids):
        return True
    return bool(info.name and info.name.upper().startswith("WB"))


class WallboxBleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Wallbox BLE config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow (the 'Configure' button on the entry)."""
        return WallboxBleOptionsFlow()

    def __init__(self) -> None:
        self._discovered_address: str | None = None
        self._discovered_name: str | None = None
        # Candidates for the manual picker: address -> label
        self._candidates: dict[str, str] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a charger discovered over Bluetooth."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovered_address = discovery_info.address
        self._discovered_name = discovery_info.name or discovery_info.address
        self.context["title_placeholders"] = {"name": self._discovered_name}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adding a discovered charger (optionally with a PIN)."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._discovered_name or "Wallbox",
                data={
                    CONF_ADDRESS: self._discovered_address,
                    CONF_PIN: user_input.get(CONF_PIN) or None,
                },
            )

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({vol.Optional(CONF_PIN): cv.string}),
            description_placeholders={
                "name": self._discovered_name or self._discovered_address
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a manual add: pick from visible Wallbox chargers."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=self._candidates.get(address, "Wallbox"),
                data={
                    CONF_ADDRESS: address,
                    CONF_PIN: user_input.get(CONF_PIN) or None,
                },
            )

        # Build the list of currently-visible Wallbox chargers.
        current = self._async_current_ids()
        self._candidates = {}
        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.address in current:
                continue
            if _is_wallbox(info):
                label = f"{info.name or 'Wallbox'} ({info.address})"
                self._candidates[info.address] = label

        if not self._candidates:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(self._candidates),
                    vol.Optional(CONF_PIN): cv.string,
                }
            ),
        )


class WallboxBleOptionsFlow(OptionsFlow):
    """Options: adjust the BLE poll interval without editing code."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL, default=current
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                    ),
                }
            ),
        )
