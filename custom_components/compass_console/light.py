"""Light platform: dimmable, RGB and RGBW lights on the IDS-CAN bus.

Plain on/off light circuits are latching relays and stay in the switch platform.
"""
from __future__ import annotations

import logging

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_HS_COLOR,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
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

_DIMMER_EFFECTS = {"Blink": P.DIM_BLINK, "Swell": P.DIM_SWELL}
_COLOR_EFFECTS = {"Blink": P.RGB_BLINK, **P.RGB_EFFECTS}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: CompassCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set = set()
    classes = {P.K_DIMMER: CompassDimmer, P.K_RGB: CompassRgbLight, P.K_RGBW: CompassRgbwLight}

    @callback
    def _sync() -> None:
        new = []
        for dev in (coordinator.data or {}).values():
            cls = classes.get(dev.kind)
            if cls and dev.key not in known:
                known.add(dev.key)
                new.append(cls(coordinator, dev))
        if new:
            async_add_entities(new)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


class _CompassLight(CoordinatorEntity[CompassCoordinator], LightEntity):
    _attr_has_entity_name = False
    _attr_supported_features = LightEntityFeature.EFFECT

    def __init__(self, coordinator: CompassCoordinator, device) -> None:
        super().__init__(coordinator)
        self._key = device.key
        self._attr_unique_id = f"{coordinator.address}_light_{device.key}"
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
        return d["on"] if d else None

    async def _send(self, command, want_on: bool) -> None:
        try:
            ok = await self.coordinator.client.async_command(
                self._key, command, lambda dev: bool(dev.data) and dev.data["on"] == want_on)
            if not ok:
                _LOGGER.debug("Compass %s: light did not confirm the change", self.name)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Compass %s command failed: %s", self.name, err)
        await self.coordinator.async_request_refresh()


class CompassDimmer(_CompassLight):
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_effect_list = list(_DIMMER_EFFECTS)

    @property
    def brightness(self) -> int | None:
        d = self._data
        if not d:
            return None
        return d["max_brightness"] or d["brightness"] or None

    @property
    def effect(self) -> str | None:
        d = self._data
        if not d:
            return None
        return next((n for n, m in _DIMMER_EFFECTS.items() if m == d["mode"]), None)

    async def async_turn_on(self, **kwargs) -> None:
        d = self._data or {}
        duration = d.get("duration", 0)
        if ATTR_EFFECT in kwargs and kwargs[ATTR_EFFECT] in _DIMMER_EFFECTS:
            level = kwargs.get(ATTR_BRIGHTNESS) or d.get("max_brightness") or 255
            cmd = P.dimmer_cmd(_DIMMER_EFFECTS[kwargs[ATTR_EFFECT]], level, duration,
                               d.get("cycle1", 0), d.get("cycle2", 0))
        elif ATTR_BRIGHTNESS in kwargs:
            cmd = P.dimmer_cmd(P.DIM_ON, max(1, int(kwargs[ATTR_BRIGHTNESS])), duration,
                               d.get("cycle1", 0), d.get("cycle2", 0))
        else:
            cmd = P.dimmer_cmd(P.DIM_RESTORE)   # back to the last brightness
        await self._send(cmd, True)

    async def async_turn_off(self, **kwargs) -> None:
        d = self._data or {}
        await self._send(P.dimmer_cmd(P.DIM_OFF, d.get("max_brightness", 0), d.get("duration", 0),
                                      d.get("cycle1", 0), d.get("cycle2", 0)), False)


class CompassRgbLight(_CompassLight):
    """RGB light. The hardware has no separate brightness; HA's brightness scales the colour."""

    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_effect_list = list(_COLOR_EFFECTS)

    @property
    def rgb_color(self):
        d = self._data
        if not d:
            return None
        r, g, b = d["rgb"]
        peak = max(r, g, b)
        if not peak:
            return (255, 255, 255)
        return tuple(round(c * 255 / peak) for c in (r, g, b))

    @property
    def brightness(self) -> int | None:
        d = self._data
        if not d:
            return None
        return max(d["rgb"]) or None

    @property
    def effect(self) -> str | None:
        d = self._data
        if not d:
            return None
        return next((n for n, m in _COLOR_EFFECTS.items() if m == d["mode"]), None)

    async def async_turn_on(self, **kwargs) -> None:
        d = self._data or {}
        duration = d.get("duration", 0)
        effect = kwargs.get(ATTR_EFFECT)
        if effect in P.RGB_EFFECTS:
            await self._send(P.rgb_cmd(P.RGB_EFFECTS[effect], (0, 0, 0), duration,
                                       P.RGB_DEFAULT_INTERVAL), True)
            return
        if ATTR_RGB_COLOR not in kwargs and ATTR_BRIGHTNESS not in kwargs and effect is None:
            await self._send(P.rgb_cmd(P.RGB_RESTORE), True)
            return
        base = kwargs.get(ATTR_RGB_COLOR) or self.rgb_color or (255, 255, 255)
        level = kwargs.get(ATTR_BRIGHTNESS, self.brightness or 255)
        color = tuple(round(c * level / 255) for c in base)
        if effect == "Blink":
            iv = (P.RGB_BLINK_INTERVAL << 8) | P.RGB_BLINK_INTERVAL
            await self._send(P.rgb_cmd(P.RGB_BLINK, color, duration, iv), True)
        else:
            await self._send(P.rgb_cmd(P.RGB_ON, color, duration, d.get("interval", 0)), True)

    async def async_turn_off(self, **kwargs) -> None:
        d = self._data or {}
        await self._send(P.rgb_cmd(P.RGB_OFF, d.get("rgb", (0, 0, 0)), d.get("duration", 0),
                                   d.get("interval", 0)), False)


class CompassRgbwLight(_CompassLight):
    """RGBW light, which the hardware drives as hue / saturation / value."""

    _attr_color_mode = ColorMode.HS
    _attr_supported_color_modes = {ColorMode.HS}
    _attr_effect_list = list(_COLOR_EFFECTS)

    @property
    def hs_color(self):
        d = self._data
        return (float(d["hue"]), float(d["saturation"])) if d else None

    @property
    def brightness(self) -> int | None:
        d = self._data
        if not d:
            return None
        return round(d["value"] * 255 / 100) or None

    @property
    def effect(self) -> str | None:
        d = self._data
        if not d:
            return None
        return next((n for n, m in _COLOR_EFFECTS.items() if m == d["mode"]), None)

    async def async_turn_on(self, **kwargs) -> None:
        d = self._data or {}
        duration = d.get("duration", 0)
        effect = kwargs.get(ATTR_EFFECT)
        if effect in P.RGB_EFFECTS:
            await self._send(P.rgbw_cmd(P.RGB_EFFECTS[effect], duration=duration,
                                        interval=P.RGB_DEFAULT_INTERVAL), True)
            return
        if ATTR_HS_COLOR not in kwargs and ATTR_BRIGHTNESS not in kwargs and effect is None:
            await self._send(P.rgbw_cmd(P.RGB_RESTORE), True)
            return
        hue, sat = kwargs.get(ATTR_HS_COLOR) or self.hs_color or (0.0, 0.0)
        level = kwargs.get(ATTR_BRIGHTNESS, self.brightness or 255)
        value = max(1, round(level * 100 / 255))
        if effect == "Blink":
            iv = (P.RGB_BLINK_INTERVAL << 8) | P.RGB_BLINK_INTERVAL
            await self._send(P.rgbw_cmd(P.RGB_BLINK, hue, sat, value, duration, iv), True)
        else:
            await self._send(P.rgbw_cmd(P.RGB_ON, hue, sat, value, duration,
                                        d.get("interval", 0)), True)

    async def async_turn_off(self, **kwargs) -> None:
        d = self._data or {}
        await self._send(P.rgbw_cmd(P.RGB_OFF, d.get("hue", 0), d.get("saturation", 0),
                                    d.get("value", 0), d.get("duration", 0),
                                    d.get("interval", 0)), False)
