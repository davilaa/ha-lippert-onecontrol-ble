"""Switch platform: each IDS-CAN latching relay (light / water heater / pump) as a switch.

Entities are keyed by FUNCTION id (stable), not CAN address (which changes when the
console loses power), so they survive power-cycles.
"""
from __future__ import annotations

import logging
from time import monotonic

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import protocol as P
from .const import DOMAIN, FUNC_ICONS, RELEASED_OPT, SWITCH_DEFAULT_ICON
from .coordinator import CompassCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: CompassCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set = set()
    used_ids: set = set()

    # One-time master control: hand the console between HA and the phone app.
    async_add_entities([CompassControlSwitch(coordinator, entry)])

    @callback
    def _sync() -> None:
        new = []
        for dev in (coordinator.data or {}).values():
            if dev.key in known:
                continue
            if dev.is_switch:
                known.add(dev.key)
                new.append(CompassSwitch(coordinator, dev, used_ids))
            elif dev.kind == P.K_GENERATOR:
                known.add(dev.key)
                new.append(CompassGeneratorSwitch(coordinator, dev))
        if new:
            async_add_entities(new)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


class CompassControlSwitch(CoordinatorEntity[CompassCoordinator], SwitchEntity):
    """Master hand-off control between Home Assistant and the Compass Connect phone app.

    The console accepts only ONE Bluetooth connection, so HA and the app can't both hold it.
    ON  = Home Assistant holds the console (normal operation).
    OFF = HA lets go and stops reconnecting, so you can open the app and use the console.
    Flip it back ON to reclaim. (If the app re-paired and evicted HA's Bluetooth bond, reclaim
    will show the link down until you re-pair with the 'Pair / Re-pair Console' button.)
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "HA Console Control"

    def __init__(self, coordinator: CompassCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{coordinator.address}_ha_control"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            name="Compass Console",
            manufacturer="Lippert / IDS",
            model="OneControl IDS-CAN gateway",
        )

    def _persist(self, released: bool) -> None:
        """Remember the released state in the config entry so it survives an HA restart."""
        options = dict(self._entry.options)
        options[RELEASED_OPT] = released
        self.hass.config_entries.async_update_entry(self._entry, options=options)

    @property
    def available(self) -> bool:
        return True  # always usable, including while released (so you can reclaim)

    @property
    def is_on(self) -> bool:
        return not self.coordinator.client.is_released  # ON = HA in control

    @property
    def icon(self) -> str:
        return "mdi:bluetooth-connect" if self.is_on else "mdi:cellphone-wireless"

    async def async_turn_on(self, **kwargs) -> None:
        """Reclaim the console for HA."""
        self._persist(False)
        try:
            await self.coordinator.client.async_reclaim()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Compass reclaim could not reconnect yet (%s); will keep retrying. If it "
                "stays down, re-pair with the Pair / Re-pair Console button.", err)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        """Release the console so the phone app can connect."""
        self._persist(True)
        await self.coordinator.client.async_release()
        _LOGGER.info("Compass released for the phone app; HA will not reconnect until reclaimed")
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()


class CompassSwitch(CoordinatorEntity[CompassCoordinator], SwitchEntity):
    """A latching relay exposed as an on/off switch."""

    _attr_has_entity_name = False

    def __init__(self, coordinator: CompassCoordinator, device, used_ids: set) -> None:
        super().__init__(coordinator)
        self._key = device.key
        self._optimistic: bool | None = None
        self._optimistic_until: float = 0.0
        # Original IDs were "<addr>_<function>"; keep that for function instance 0 so existing
        # installs keep their entities, and disambiguate everything else.
        uid = f"{coordinator.address}_{device.func}"
        if device.finst:
            uid = f"{uid}_{device.finst}"
        if uid in used_ids:
            uid = f"{coordinator.address}_{device.key}"
        used_ids.add(uid)
        self._attr_unique_id = uid
        self._attr_name = device.name
        self._attr_icon = FUNC_ICONS.get(device.func, SWITCH_DEFAULT_ICON)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            name="Compass Console",
            manufacturer="Lippert / IDS",
            model="OneControl IDS-CAN gateway",
        )

    @property
    def _device(self):
        return (self.coordinator.data or {}).get(self._key)

    @property
    def available(self) -> bool:
        return super().available and self._device is not None

    @property
    def is_on(self) -> bool | None:
        dev = self._device
        real = dev.is_on if dev else None
        if self._optimistic is not None:
            # Show the commanded state until the device actually reports it. Never fall back
            # to a stale reading (that caused the "on is instant, off lags" flip-back: relays
            # go quiet after the load drops, so the fresh "off" broadcast arrives late). A
            # failsafe expiry keeps a failed command from sticking forever.
            if real is not None and real == self._optimistic:
                self._optimistic = None
                return real
            if monotonic() >= self._optimistic_until:
                self._optimistic = None
                return real
            return self._optimistic
        return real

    async def async_turn_on(self, **kwargs) -> None:
        self._apply(True)

    async def async_turn_off(self, **kwargs) -> None:
        self._apply(False)

    def _apply(self, on: bool) -> None:
        # Flip the UI immediately, then run the (slower) BLE command in the background.
        self._optimistic = on
        self._optimistic_until = monotonic() + 30.0
        self.async_write_ha_state()
        self.hass.async_create_task(self._bg_apply(on))

    async def _bg_apply(self, on: bool) -> None:
        try:
            await self.coordinator.client.async_set(f"key:{self._key}", on)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Compass %s turn %s failed: %s",
                          self._attr_name, "on" if on else "off", err)
            self._optimistic = None  # command failed -> defer to the device's real state
        await self.coordinator.async_request_refresh()
        self.async_write_ha_state()


class CompassGeneratorSwitch(CoordinatorEntity[CompassCoordinator], SwitchEntity):
    """Generator Genie: ON starts the generator (one press), OFF stops it."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:engine"

    def __init__(self, coordinator: CompassCoordinator, device) -> None:
        super().__init__(coordinator)
        self._key = device.key
        self._attr_unique_id = f"{coordinator.address}_generator_{device.key}"
        self._attr_name = device.name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            name="Compass Console",
            manufacturer="Lippert / IDS",
            model="OneControl IDS-CAN gateway",
        )

    @property
    def _data(self):
        dev = (self.coordinator.data or {}).get(self._key)
        return dev.data if dev else None

    @property
    def available(self) -> bool:
        return super().available and self._data is not None

    @property
    def is_on(self) -> bool | None:
        d = self._data
        return d["state"] in ("priming", "starting", "running") if d else None

    @property
    def extra_state_attributes(self) -> dict:
        d = self._data or {}
        return {"generator_state": d.get("state"), "quiet_hours": d.get("quiet_hours")}

    async def async_turn_on(self, **kwargs) -> None:
        await self._send(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._send(False)

    async def _send(self, on: bool) -> None:
        def confirm(dev) -> bool:
            d = dev.data
            return bool(d) and ((d["state"] != "off") if on else (d["state"] == "off"))

        try:
            ok = await self.coordinator.client.async_command(
                self._key, P.generator_cmd(on), confirm, timeout=5.0)
            if not ok:
                _LOGGER.warning("Compass %s: generator did not confirm %s",
                                self._attr_name, "start" if on else "stop")
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Compass %s %s failed: %s", self._attr_name,
                          "start" if on else "stop", err)
        await self.coordinator.async_request_refresh()
