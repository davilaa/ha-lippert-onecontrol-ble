"""Config + options flow for the Compass Console integration.

Per-cover travel time and position calibration are exposed as Number entities in each
device's Configuration section (see number.py), not here. This options flow only carries
the global scan interval.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ADDRESS,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_SCAN_LABEL = "Scan interval (seconds)"


class CompassConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Compass Console."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered_address: str | None = None
        self._discovered_name: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return CompassOptionsFlow()

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovered_address = discovery_info.address
        self._discovered_name = discovery_info.name
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_user()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=self._discovered_name or "Compass Console",
                data=user_input,
            )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ADDRESS,
                    default=self._discovered_address or "",
                ): str,
                vol.Optional(
                    CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                ): int,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)


class CompassOptionsFlow(OptionsFlow):
    """Global scan interval. Per-cover calibration lives on the device page (numbers)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self.config_entry
        if user_input is not None:
            new_options = dict(entry.options)
            new_options[CONF_SCAN_INTERVAL] = int(user_input[_SCAN_LABEL])
            return self.async_create_entry(title="", data=new_options)

        default_scan = int(
            entry.options.get(
                CONF_SCAN_INTERVAL,
                entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            )
        )
        schema = vol.Schema(
            {
                vol.Optional(_SCAN_LABEL, default=default_scan): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=15, max=3600, step=5,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="s",
                    )
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
