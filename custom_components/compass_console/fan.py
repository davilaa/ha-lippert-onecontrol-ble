"""Fan platform: roof fans on the IDS-CAN bus (e.g. MaxxFan). Their lid is in cover.py."""
from __future__ import annotations

import logging

from homeassistant.components.fan import (
    DIRECTION_FORWARD,
    DIRECTION_REVERSE,
    FanEntity,
    FanEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import protocol as P
from .const import DOMAIN
from .coordinator import CompassCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: CompassCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set = set()

    @callback
    def _sync() -> None:
        new = []
        for dev in (coordinator.data or {}).values():
            if dev.kind == P.K_FAN and dev.key not in known:
                known.add(dev.key)
                new.append(CompassFan(coordinator, dev))
        if new:
            async_add_entities(new)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


class CompassFan(CoordinatorEntity[CompassCoordinator], FanEntity):
    _attr_has_entity_name = False
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED | FanEntityFeature.DIRECTION
        | FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator: CompassCoordinator, device) -> None:
        super().__init__(coordinator)
        self._key = device.key
        self._attr_unique_id = f"{coordinator.address}_fan_{device.key}"
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
        return d["speed"] > 0 if d else None

    @property
    def percentage(self) -> int | None:
        d = self._data
        return d["speed"] if d else None

    @property
    def current_direction(self) -> str | None:
        d = self._data
        if not d:
            return None
        return DIRECTION_REVERSE if d["direction"] == "reverse" else DIRECTION_FORWARD

    @property
    def extra_state_attributes(self) -> dict:
        d = self._data or {}
        return {"rain_detected": d.get("rain_detected")}

    async def _send(self, command, confirm) -> None:
        try:
            if not await self.coordinator.client.async_command(self._key, command, confirm):
                _LOGGER.debug("Compass %s: fan did not confirm the change", self.name)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Compass %s fan command failed: %s", self.name, err)
        await self.coordinator.async_request_refresh()

    async def async_set_percentage(self, percentage: int) -> None:
        reverse = self.current_direction == DIRECTION_REVERSE
        await self._send(P.fan_speed_cmd(percentage, reverse),
                         lambda dev: bool(dev.data) and dev.data["speed"] == percentage)

    async def async_turn_on(self, percentage: int | None = None, preset_mode: str | None = None,
                            **kwargs) -> None:
        if percentage is None:
            await self._send((P.FAN_CMD_RESTORE, b"\x00"),
                             lambda dev: bool(dev.data) and dev.data["speed"] > 0)
        else:
            await self.async_set_percentage(percentage)

    async def async_turn_off(self, **kwargs) -> None:
        await self.async_set_percentage(0)

    async def async_set_direction(self, direction: str) -> None:
        speed = self.percentage or 0
        reverse = direction == DIRECTION_REVERSE
        want = "reverse" if reverse else "forward"
        await self._send(P.fan_speed_cmd(speed, reverse),
                         lambda dev: bool(dev.data) and (dev.data["speed"] == 0
                                                         or dev.data["direction"] == want))
