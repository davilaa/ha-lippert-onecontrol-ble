"""Climate platform: OneControl HVAC zones (thermostats)."""
from __future__ import annotations

import logging

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import protocol as P
from .const import DOMAIN
from .coordinator import CompassCoordinator

_LOGGER = logging.getLogger(__name__)

_MODE_TO_HA = {
    P.HVAC_OFF: HVACMode.OFF, P.HVAC_HEAT: HVACMode.HEAT, P.HVAC_COOL: HVACMode.COOL,
    P.HVAC_BOTH: HVACMode.HEAT_COOL, P.HVAC_SCHEDULE: HVACMode.AUTO,
}
_HA_TO_MODE = {v: k for k, v in _MODE_TO_HA.items()}
_FAN_TO_HA = {P.FAN_AUTO: "auto", P.FAN_HIGH: "high", P.FAN_LOW: "low"}
_HA_TO_FAN = {v: k for k, v in _FAN_TO_HA.items()}
_SOURCE_TO_PRESET = {P.SRC_GAS: "prefer_gas", P.SRC_HEAT_PUMP: "prefer_heat_pump",
                     P.SRC_OTHER: "other"}
_PRESET_TO_SOURCE = {v: k for k, v in _SOURCE_TO_PRESET.items()}
_ACTION = {
    "off": HVACAction.OFF, "idle": HVACAction.IDLE, "cooling": HVACAction.COOLING,
    "heating_heat_pump": HVACAction.HEATING, "heating_electric": HVACAction.HEATING,
    "heating_gas": HVACAction.HEATING, "heating_gas_override": HVACAction.HEATING,
    "dead_time": HVACAction.IDLE, "load_shedding": HVACAction.IDLE,
}


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
            if dev.kind == P.K_HVAC and dev.key not in known:
                known.add(dev.key)
                new.append(CompassClimate(coordinator, dev))
        if new:
            async_add_entities(new)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


class CompassClimate(CoordinatorEntity[CompassCoordinator], ClimateEntity):
    _attr_has_entity_name = False
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_target_temperature_step = 1
    _attr_min_temp = 47
    _attr_max_temp = 92
    _attr_fan_modes = list(_HA_TO_FAN)

    def __init__(self, coordinator: CompassCoordinator, device) -> None:
        super().__init__(coordinator)
        self._key = device.key
        self._attr_unique_id = f"{coordinator.address}_climate_{device.key}"
        self._attr_name = device.name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            name="Compass Console",
            manufacturer="Lippert / IDS",
            model="OneControl IDS-CAN gateway",
        )
        caps = device.caps or 0
        unknown = caps == 0   # capability not reported: offer everything
        can_heat = unknown or bool(caps & (P.HVAC_CAP_GAS | P.HVAC_CAP_HEAT_PUMP))
        can_cool = unknown or bool(caps & P.HVAC_CAP_AC)
        modes = [HVACMode.OFF]
        if can_heat:
            modes.append(HVACMode.HEAT)
        if can_cool:
            modes.append(HVACMode.COOL)
        if can_heat and can_cool:
            modes.append(HVACMode.HEAT_COOL)
        modes.append(HVACMode.AUTO)
        self._attr_hvac_modes = modes
        self._presets = None
        if unknown or (caps & P.HVAC_CAP_GAS and caps & P.HVAC_CAP_HEAT_PUMP):
            self._presets = ["prefer_gas", "prefer_heat_pump"]
        self._attr_preset_modes = self._presets

    @property
    def _data(self):
        dev = (self.coordinator.data or {}).get(self._key)
        return dev.data if dev else None

    @property
    def available(self) -> bool:
        return super().available and self._data is not None

    @property
    def supported_features(self) -> ClimateEntityFeature:
        f = ClimateEntityFeature.FAN_MODE | ClimateEntityFeature.TURN_OFF | \
            ClimateEntityFeature.TURN_ON
        mode = self.hvac_mode
        if mode == HVACMode.HEAT_COOL:
            f |= ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        elif mode in (HVACMode.HEAT, HVACMode.COOL):
            f |= ClimateEntityFeature.TARGET_TEMPERATURE
        if self._presets:
            f |= ClimateEntityFeature.PRESET_MODE
        return f

    @property
    def hvac_mode(self) -> HVACMode | None:
        d = self._data
        return _MODE_TO_HA.get(d["heat_mode"], HVACMode.OFF) if d else None

    @property
    def hvac_action(self) -> HVACAction | None:
        d = self._data
        return _ACTION.get(d["zone_status"]) if d else None

    @property
    def current_temperature(self) -> float | None:
        d = self._data
        return d["indoor_f"] if d else None

    @property
    def target_temperature(self) -> float | None:
        d = self._data
        if not d:
            return None
        if d["heat_mode"] == P.HVAC_HEAT:
            return d["low_trip_f"]
        if d["heat_mode"] == P.HVAC_COOL:
            return d["high_trip_f"]
        return None

    @property
    def target_temperature_low(self) -> float | None:
        d = self._data
        return d["low_trip_f"] if d else None

    @property
    def target_temperature_high(self) -> float | None:
        d = self._data
        return d["high_trip_f"] if d else None

    @property
    def fan_mode(self) -> str | None:
        d = self._data
        return _FAN_TO_HA.get(d["fan_mode"]) if d else None

    @property
    def preset_mode(self) -> str | None:
        d = self._data
        return _SOURCE_TO_PRESET.get(d["heat_source"]) if d and self._presets else None

    @property
    def extra_state_attributes(self) -> dict:
        d = self._data or {}
        return {"outdoor_temperature": d.get("outdoor_f"), "zone_status": d.get("zone_status"),
                "failed_thermistor": d.get("failed_thermistor")}

    async def _apply(self, **changes) -> None:
        d = self._data
        if not d:
            raise RuntimeError("HVAC zone status not available yet")
        mode = changes.get("mode", d["heat_mode"])
        source = changes.get("source", d["heat_source"])
        fan = changes.get("fan", d["fan_mode"])
        low = int(changes.get("low", d["low_trip_f"]))
        high = int(changes.get("high", d["high_trip_f"]))
        cmd = P.hvac_cmd(mode, source, fan, low, high)

        def confirm(dev) -> bool:
            s = dev.data
            return bool(s) and s["heat_mode"] == mode and s["heat_source"] == source and \
                s["fan_mode"] == fan and s["low_trip_f"] == low and s["high_trip_f"] == high

        try:
            if not await self.coordinator.client.async_command(self._key, cmd, confirm, 5.0):
                _LOGGER.warning("Compass %s: HVAC did not confirm the new settings", self.name)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Compass %s HVAC command failed: %s", self.name, err)
        await self.coordinator.async_request_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self._apply(mode=_HA_TO_MODE[hvac_mode])

    async def async_turn_on(self) -> None:
        d = self._data or {}
        if d.get("heat_mode", P.HVAC_OFF) == P.HVAC_OFF:
            await self._apply(mode=P.HVAC_SCHEDULE if len(self.hvac_modes) <= 2 else
                              _HA_TO_MODE[self.hvac_modes[1]])

    async def async_turn_off(self) -> None:
        await self._apply(mode=P.HVAC_OFF)

    async def async_set_temperature(self, **kwargs) -> None:
        changes = {}
        if ATTR_HVAC_MODE in kwargs:
            changes["mode"] = _HA_TO_MODE[kwargs[ATTR_HVAC_MODE]]
        mode = changes.get("mode", (self._data or {}).get("heat_mode"))
        if ATTR_TEMPERATURE in kwargs:
            key = "high" if mode == P.HVAC_COOL else "low"
            changes[key] = kwargs[ATTR_TEMPERATURE]
        if ATTR_TARGET_TEMP_LOW in kwargs:
            changes["low"] = kwargs[ATTR_TARGET_TEMP_LOW]
        if ATTR_TARGET_TEMP_HIGH in kwargs:
            changes["high"] = kwargs[ATTR_TARGET_TEMP_HIGH]
        await self._apply(**changes)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        await self._apply(fan=_HA_TO_FAN[fan_mode])

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        await self._apply(source=_PRESET_TO_SOURCE[preset_mode])
