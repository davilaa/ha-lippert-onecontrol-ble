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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import protocol as P
from .compass_ble import MOVE_FORWARD, MOVE_REVERSE
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
    known: set = set()

    @callback
    def _sync() -> None:
        new = []
        for dev in (coordinator.data or {}).values():
            if dev.kind == P.K_RELAY_GENERATOR and dev.key not in known:
                known.add(dev.key)
                new += [CompassGeneratorPress(coordinator, dev, True),
                        CompassGeneratorPress(coordinator, dev, False)]
        if new:
            async_add_entities(new)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


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


GEN_PRESS_MAX_SECONDS = 10.0   # longest we hold Start / Stop
GEN_PRESS_BLIND_SECONDS = 3.0  # hold time when no hour meter reports the running state


class CompassGeneratorPress(CoordinatorEntity[CompassCoordinator], ButtonEntity):
    """Start or Stop for a generator wired through an h-bridge relay. The app only offers a
    press-and-hold button for these, so this holds the button until an hour meter on the bus
    reports the generator running (Start) / stopped (Stop), or for a fixed short press if
    there is no hour meter."""

    def __init__(self, coordinator: CompassCoordinator, device, start: bool) -> None:
        super().__init__(coordinator)
        self._key = device.key
        self._start = start
        word = "Start" if start else "Stop"
        self._attr_unique_id = f"{coordinator.address}_gen_{word.lower()}_{device.key}"
        self._attr_name = f"{device.name} {word}"
        self._attr_icon = "mdi:engine" if start else "mdi:engine-off"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            name="Compass Console",
            manufacturer="Lippert / IDS",
            model="OneControl IDS-CAN gateway",
        )

    def _running(self):
        """Running state from the hour meter on the same module as this relay (the app's
        pairing rule: same MAC + product id), or None if there isn't one."""
        devices = self.coordinator.client.devices
        me = next((d for d in devices if d.key == self._key), None)
        if me is None:
            return None
        for d in devices:
            if d.kind == P.K_HOUR_METER and d.mac == me.mac and d.product_id == me.product_id:
                data = d.data
                if data and data["valid"]:
                    return data["running"]
        return None

    async def async_press(self) -> None:
        has_meter = self._running() is not None
        seconds = GEN_PRESS_MAX_SECONDS if has_meter else GEN_PRESS_BLIND_SECONDS
        want = self._start

        def done() -> bool:
            running = self._running()
            return running is not None and running == want

        try:
            await self.coordinator.client.async_move(
                self._key, MOVE_FORWARD if self._start else MOVE_REVERSE, seconds,
                max_seconds=GEN_PRESS_MAX_SECONDS, until=done)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Compass %s failed: %s", self.name, err)
        await self.coordinator.async_request_refresh()
