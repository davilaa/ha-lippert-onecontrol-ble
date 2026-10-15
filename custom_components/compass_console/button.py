"""Button platform: a manual Pair / Re-pair button for the console.

Put the console into Bluetooth pairing mode (flashing), then press this button. It clears
any stale bond and re-pairs from Home Assistant, so you don't need shell access to recover
if the console's bond is lost (e.g. after a phone re-pairs to it).
"""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import CompassCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: CompassCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        CompassPairButton(coordinator),
        CompassStopButton(coordinator),
        CompassClearFaultButton(coordinator),
    ])


class CompassPairButton(ButtonEntity):
    """Manually pair / re-pair the console's Bluetooth bond."""

    _attr_name = "Pair / Re-pair Console"
    _attr_icon = "mdi:bluetooth-connect"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: CompassCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.address}_pair"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            name="Compass Console",
            manufacturer="Lippert / IDS",
            model="OneControl IDS-CAN gateway",
        )

    async def async_press(self) -> None:
        try:
            ok = await self._coordinator.client.async_pair()
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Compass pairing failed: %s", err)
            return
        _LOGGER.info(
            "Compass pairing %s",
            "succeeded" if ok else "did not complete (put console in pairing mode and retry)",
        )
        if ok:
            await self._coordinator.async_request_refresh()


class CompassStopButton(ButtonEntity):
    """Emergency stop -- immediately halt any slide/awning movement in progress."""

    _attr_name = "Emergency Stop"
    _attr_icon = "mdi:stop-circle-outline"

    def __init__(self, coordinator: CompassCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.address}_estop"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            name="Compass Console",
            manufacturer="Lippert / IDS",
            model="OneControl IDS-CAN gateway",
        )

    async def async_press(self) -> None:
        # Trip every in-flight move at once; the momentary motors stop the instant their pulse
        # stream stops, and each move sends an explicit STOP as it breaks.
        n = self._coordinator.client.async_stop_all()
        _LOGGER.warning("Compass emergency stop -- halting %d moving cover(s)", n)


class CompassClearFaultButton(ButtonEntity):
    """Clear a latched output fault (OutputDisabledLatch, e.g. over-current) on ANY device --
    relays and h-bridge covers alike -- via command mode 3, the same clear the OneControl panel
    sends. No power cycle needed. Only devices actually reporting a fault are touched."""

    _attr_name = "Clear All Faults"
    _attr_icon = "mdi:alert-remove-outline"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: CompassCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.address}_clearfault"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            name="Compass Console",
            manufacturer="Lippert / IDS",
            model="OneControl IDS-CAN gateway",
        )

    async def async_press(self) -> None:
        try:
            n = await self._coordinator.client.async_clear_all_faults()
            _LOGGER.info("Compass clear-fault: cleared %d device fault latch(es)", n)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Compass clear-fault failed: %s", err)
        await self._coordinator.async_request_refresh()
