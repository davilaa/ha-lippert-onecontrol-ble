"""Number platform: per-cover calibration controls, shown in the device's
Configuration section alongside the Pair / Re-pair button.

For each slide/awning:
  * "<name> Position"    -- a slider to RESET the estimated position when it drifts from
                            reality. Setting it re-syncs the estimate WITHOUT moving the motor.
  * "<name> Travel Time" -- seconds for a full open<->close travel (the basis for the
                            percent estimate). Persisted in the config entry options.
"""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    COVER_STALL_FUNCS,
    COVER_STALL_OPT,
    COVER_TRAVEL_OPT,
    DOMAIN,
    SIGNAL_COVER_POS,
)
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
        new: list[NumberEntity] = []
        for dev in (coordinator.data or {}).values():
            if not dev.is_cover:
                continue
            if ("pos:" + dev.key) not in known:
                known.add("pos:" + dev.key)
                new.append(CompassPositionNumber(coordinator, dev))
            if ("topen:" + dev.key) not in known:
                known.add("topen:" + dev.key)
                new.append(CompassTravelNumber(coordinator, entry, dev, opening=True))
            if ("tclose:" + dev.key) not in known:
                known.add("tclose:" + dev.key)
                new.append(CompassTravelNumber(coordinator, entry, dev, opening=False))
            if dev.func in COVER_STALL_FUNCS and ("stall:" + dev.key) not in known:
                known.add("stall:" + dev.key)
                new.append(CompassStallNumber(coordinator, entry, dev))
        if new:
            async_add_entities(new)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


def _device_info(coordinator: CompassCoordinator) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.address)},
        name="Compass Console",
        manufacturer="Lippert / IDS",
        model="OneControl IDS-CAN gateway",
    )


class CompassPositionNumber(CoordinatorEntity[CompassCoordinator], NumberEntity):
    """Reset/re-sync a cover's estimated position (does not move the motor)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:arrow-expand-horizontal"

    def __init__(self, coordinator: CompassCoordinator, device) -> None:
        super().__init__(coordinator)
        self._key = device.key
        self._attr_unique_id = f"{coordinator.address}_setpos_{device.key}"
        self._attr_name = f"{device.name} Position"
        self._attr_device_info = _device_info(coordinator)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Track the cover's live estimated position as it moves.
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"{SIGNAL_COVER_POS}_{self._key}", self._cover_updated
            )
        )

    @callback
    def _cover_updated(self) -> None:
        self.async_write_ha_state()

    @property
    def _cover(self):
        return self.coordinator.covers.get(self._key)

    @property
    def available(self) -> bool:
        return super().available and self._cover is not None

    @property
    def native_value(self) -> float | None:
        cover = self._cover
        if cover is None:
            return None
        pos = cover.current_cover_position
        return None if pos is None else float(pos)

    async def async_set_native_value(self, value: float) -> None:
        cover = self._cover
        if cover is not None:
            cover.set_estimated_position(int(value))
        self.async_write_ha_state()


class CompassTravelNumber(CoordinatorEntity[CompassCoordinator], NumberEntity):
    """Open- or Close-direction full-travel time (seconds) for the percent estimate.

    Awnings extend and retract at different speeds, so each direction is timed separately.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 1
    _attr_native_max_value = 90
    _attr_native_step = 0.1  # tenths of a second, for fine position calibration
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS

    def __init__(
        self, coordinator: CompassCoordinator, entry: ConfigEntry, device, opening: bool
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._key = device.key
        self._opening = opening
        word = "Open" if opening else "Close"
        self._attr_icon = "mdi:timer-arrow-up" if opening else "mdi:timer-arrow-down"
        # "Open" reuses the legacy single-travel unique_id so it migrates in place; "Close" is new.
        if opening:
            self._attr_unique_id = f"{coordinator.address}_travel_{device.key}"
        else:
            self._attr_unique_id = f"{coordinator.address}_travel_close_{device.key}"
        self._attr_name = f"{device.name} {word} Travel Time"
        self._attr_device_info = _device_info(coordinator)

    @property
    def native_value(self) -> float:
        return self.coordinator.get_travel(self._key, self._opening)

    async def async_set_native_value(self, value: float) -> None:
        kind = "open" if self._opening else "close"
        val = float(value)
        # Update the live value (preserving the other direction), coercing a legacy single float.
        cur = self.coordinator.cover_travel.get(self._key)
        if isinstance(cur, dict):
            cur = dict(cur)
        elif isinstance(cur, (int, float)):
            cur = {"open": float(cur), "close": float(cur)}
        else:
            cur = {}
        cur[kind] = val
        self.coordinator.cover_travel[self._key] = cur
        # Persist to the config entry options.
        options = dict(self._entry.options)
        travel = dict(options.get(COVER_TRAVEL_OPT, {}))
        travel[self._key] = cur
        options[COVER_TRAVEL_OPT] = travel
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        self.async_write_ha_state()


class CompassStallNumber(CoordinatorEntity[CompassCoordinator], NumberEntity):
    """Motor-current threshold (amps) that marks a physical end-stop.

    Closing drives until the current holds above this (fully retracted); opening uses it as an
    over-travel safeguard. Measured references: ~6A running / ~14A stall (closing), ~8.5A at the
    open limit -- so a value below the limits but above running current (default 7A) works.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 1
    _attr_native_max_value = 30
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_icon = "mdi:flash-alert"

    def __init__(self, coordinator: CompassCoordinator, entry: ConfigEntry, device) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._key = device.key
        self._attr_unique_id = f"{coordinator.address}_stall_{device.key}"
        self._attr_name = f"{device.name} Stall Current"
        self._attr_device_info = _device_info(coordinator)

    @property
    def native_value(self) -> float:
        return self.coordinator.get_stall_amps(self._key)

    async def async_set_native_value(self, value: float) -> None:
        val = float(value)
        self.coordinator.cover_stall[self._key] = val
        options = dict(self._entry.options)
        stall = dict(options.get(COVER_STALL_OPT, {}))
        stall[self._key] = val
        options[COVER_STALL_OPT] = stall
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        self.async_write_ha_state()
