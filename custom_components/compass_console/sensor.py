"""Sensor platform: tank fill levels (Fresh / Grey / Black) as percentage sensors."""
from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfElectricCurrent
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, TANK_DEFAULT_ICON, TANK_ICONS
from .coordinator import CompassCoordinator

try:
    from .tables import DTC
except Exception:  # pragma: no cover
    DTC = {}

_LOGGER = logging.getLogger(__name__)


def _describe_dtc(code: int) -> str:
    """Human-readable translation of a raw DTC code."""
    name = DTC.get(code)
    if name:
        return name.replace("_", " ").capitalize()
    return f"Unknown fault code {code}"


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
        if "status" not in known:  # one console-wide connection/pairing status sensor
            known.add("status")
            new.append(CompassStatusSensor(coordinator))
        if "faults" not in known:  # one console-wide fault sensor
            known.add("faults")
            new.append(CompassFaultSensor(coordinator))
        for dev in (coordinator.data or {}).values():
            if dev.is_tank and ("tank:" + dev.key) not in known:
                known.add("tank:" + dev.key)
                new.append(CompassTankSensor(coordinator, dev))
            if dev.current is not None and ("cur:" + dev.key) not in known:
                known.add("cur:" + dev.key)
                new.append(CompassCurrentSensor(coordinator, dev))
        if new:
            async_add_entities(new)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


class CompassStatusSensor(CoordinatorEntity[CompassCoordinator], SensorEntity):
    """Console link / pairing status. 'Connected' means paired and communicating; anything
    else means the console isn't reachable (unpaired, powered off, or out of range)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["Connected", "Link up (no data)", "Disconnected"]

    def __init__(self, coordinator: CompassCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_status"
        self._attr_name = "Connection Status"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            name="Compass Console",
            manufacturer="Lippert / IDS",
            model="OneControl IDS-CAN gateway",
        )

    @property
    def available(self) -> bool:
        return True  # always show status, even when the link is down

    @property
    def native_value(self) -> str:
        ok = self.coordinator.last_update_success
        n = len(self.coordinator.data or {})
        if ok and n:
            return "Connected"
        if getattr(self.coordinator.client, "is_connected", False):
            return "Link up (no data)"
        return "Disconnected"

    @property
    def icon(self) -> str:
        return "mdi:bluetooth-connect" if self.native_value == "Connected" else "mdi:bluetooth-off"

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "bonded_address": self.coordinator.address,
            "ble_link_up": getattr(self.coordinator.client, "is_connected", False),
            "last_poll_ok": self.coordinator.last_update_success,
            "devices_seen": len(self.coordinator.data or {}),
        }


class CompassTankSensor(CoordinatorEntity[CompassCoordinator], SensorEntity):
    """A tank fill level reported as a percentage."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: CompassCoordinator, device) -> None:
        super().__init__(coordinator)
        self._key = device.key
        self._attr_unique_id = f"{coordinator.address}_tank_{device.key}"
        self._attr_name = device.name
        self._attr_icon = TANK_ICONS.get(device.func, TANK_DEFAULT_ICON)
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
        dev = self._device
        return super().available and dev is not None and dev.fill_level is not None

    @property
    def native_value(self) -> int | None:
        dev = self._device
        return dev.fill_level if dev else None


class CompassCurrentSensor(CoordinatorEntity[CompassCoordinator], SensorEntity):
    """Output current draw (amps) of a relay / h-bridge load."""

    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: CompassCoordinator, device) -> None:
        super().__init__(coordinator)
        self._key = device.key
        self._attr_unique_id = f"{coordinator.address}_current_{device.key}"
        self._attr_name = f"{device.name} Current"
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
        dev = self._device
        return super().available and dev is not None and dev.current is not None

    @property
    def native_value(self) -> float | None:
        dev = self._device
        return round(dev.current, 2) if dev and dev.current is not None else None


class CompassFaultSensor(CoordinatorEntity[CompassCoordinator], SensorEntity):
    """One console-wide sensor that surfaces any active device fault.

    State is "OK" or a short summary with the raw code; the ``faults`` attribute lists each
    fault with its device, raw code (hex + decimal), firmware DTC name and a description.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: CompassCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_faults"
        self._attr_name = "Fault Status"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            name="Compass Console",
            manufacturer="Lippert / IDS",
            model="OneControl IDS-CAN gateway",
        )

    def _faults(self) -> list[dict]:
        out: list[dict] = []
        for dev in (self.coordinator.data or {}).values():
            code = getattr(dev, "dtc_code", 0) or 0
            latched = getattr(dev, "is_faulted", False)
            if not code and not latched:
                continue
            out.append(
                {
                    "device": dev.name,
                    "code": f"0x{code:04X}",
                    "code_decimal": code,
                    "dtc": DTC.get(code, "UNKNOWN"),
                    "description": _describe_dtc(code) if code else "Output disabled (fault latched)",
                }
            )
        return out

    @property
    def icon(self) -> str:
        return "mdi:alert-circle" if self._faults() else "mdi:check-circle-outline"

    @property
    def native_value(self) -> str:
        faults = self._faults()
        if not faults:
            return "OK"
        summary = "; ".join(
            f"{f['device']}: {f['description']} ({f['code']})" for f in faults
        )
        return summary[:255]

    @property
    def extra_state_attributes(self) -> dict:
        faults = self._faults()
        return {"fault_count": len(faults), "faults": faults}
