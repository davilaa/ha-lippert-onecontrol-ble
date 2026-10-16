"""Cover platform: slides and awnings (momentary H-bridge motors).

These are momentary devices with NO position feedback (the gateway reports 0xff for
OutputPositionPct on these motors). So position is ESTIMATED from motor run-time:
a full closed<->open travel is assumed to take COVER_FULL_TRAVEL_SECONDS (tunable per
function in const.COVER_TRAVEL_SECONDS), and "open to X%" simply drives the motor for the
matching fraction of that time. Fully opening/closing overshoots slightly to firmly hit the
physical limit, which re-calibrates the estimate to exactly 100 / 0.

The motor is a fail-safe momentary: it only moves while it is actively fed commands, so a
disconnect or a stop halts it immediately.
"""
from __future__ import annotations

import asyncio
import logging
from time import monotonic

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import protocol as P
from .compass_ble import MOVE_FORWARD, MOVE_REVERSE
from .const import (
    COVER_DEFAULT_ICON,
    COVER_ICONS,
    COVER_MAX_MOVE_SECONDS,
    COVER_STALL_ARM_SECONDS,
    COVER_STALL_FUNCS,
    COVER_STALL_HOLD_SECONDS,
    DOMAIN,
    SIGNAL_COVER_POS,
)
from .coordinator import CompassCoordinator

_LOGGER = logging.getLogger(__name__)

# FUNCTION_CLASS -> cover device class (slides and anything else get the generic class).
_CLASS_DEVICE_CLASS = {
    2: CoverDeviceClass.AWNING,      # AWNING
    27: CoverDeviceClass.DOOR,       # DOOR (incl. ramp doors)
    14: CoverDeviceClass.DAMPER,     # VENT_COVER
}
# Jacks: exposed like slides (the app drives them the same way) but disabled by default, so
# nobody can move a jack from HA without deliberately enabling the entity first.
_DISABLED_BY_DEFAULT_CLASSES = {6, 24}   # LANDING_GEAR, STABILIZER


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
            if dev.key in known:
                continue
            if dev.is_cover:
                known.add(dev.key)
                new.append(CompassCover(coordinator, dev))
            elif dev.kind == P.K_FAN and dev.caps & 0x11:   # fan with a powered lid
                known.add(dev.key)
                new.append(CompassVentLid(coordinator, dev))
        if new:
            async_add_entities(new)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


class CompassCover(CoordinatorEntity[CompassCoordinator], RestoreEntity, CoverEntity):
    """A slide or awning driven by a momentary H-bridge, with time-estimated position."""

    _attr_assumed_state = True  # position is estimated from run-time, not measured
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, coordinator: CompassCoordinator, device) -> None:
        super().__init__(coordinator)
        self._key = device.key
        # Estimated position, 0 = fully closed/retracted, 100 = fully open/extended.
        self._position: float | None = None
        self._moving: str | None = None  # "opening" | "closing" | None
        self._target: float | None = None
        self._move_start_pos: float = 0.0
        self._move_dir_sign: float = 1.0
        self._move_started: float = 0.0
        self._move_seconds: float = 0.0
        self._move_full: float = float(coordinator.default_travel)  # travel time of active move
        self._full_end: bool = False
        self._stall_capable: bool = device.func in COVER_STALL_FUNCS  # awnings: known signature
        self._detect_stall: bool = False  # armed for the current move
        self._run_task: asyncio.Task | None = None
        self._ticker: asyncio.Task | None = None

        self._attr_unique_id = f"{coordinator.address}_cover_{device.key}"
        self._attr_name = device.name
        self._attr_icon = COVER_ICONS.get(device.func, COVER_DEFAULT_ICON)
        if device.fclass in _CLASS_DEVICE_CLASS:
            self._attr_device_class = _CLASS_DEVICE_CLASS[device.fclass]
        if device.fclass in _DISABLED_BY_DEFAULT_CLASSES:
            self._attr_entity_registry_enabled_default = False
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            name="Compass Console",
            manufacturer="Lippert / IDS",
            model="OneControl IDS-CAN gateway",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            try:
                p = last.attributes.get("current_position")
                if p is not None:
                    self._position = max(0.0, min(100.0, float(p)))
            except (TypeError, ValueError):
                pass
        if self._position is None:
            # No history: assume retracted/closed. A full open+close re-calibrates it.
            self._position = 0.0
        # Register so the Options flow can re-calibrate this cover's estimated position.
        self.coordinator.covers[self._key] = self

    async def async_will_remove_from_hass(self) -> None:
        if self.coordinator.covers.get(self._key) is self:
            self.coordinator.covers.pop(self._key, None)
        await super().async_will_remove_from_hass()

    @callback
    def set_estimated_position(self, pct: float) -> None:
        """Re-sync the estimated position to reality (halts any motion; does not move)."""
        self.coordinator.client.async_stop(self._key)
        self._moving = None
        self._target = None
        self._position = max(0.0, min(100.0, float(pct)))
        self._write_state()

    @callback
    def _write_state(self) -> None:
        """Write our state and notify the paired Position number entity to refresh live."""
        self.async_write_ha_state()
        async_dispatcher_send(self.hass, f"{SIGNAL_COVER_POS}_{self._key}")

    @property
    def _device(self):
        return (self.coordinator.data or {}).get(self._key)

    @property
    def available(self) -> bool:
        return super().available and self._device is not None

    def _estimate(self, elapsed: float, seconds: float) -> float:
        """Position after running ``elapsed`` s of an intended ``seconds`` run."""
        start = self._move_start_pos
        full = self._move_full if self._move_full > 0 else 1.0
        raw = start + self._move_dir_sign * (elapsed / full) * 100.0
        tgt = self._target if self._target is not None else raw
        if self._move_dir_sign > 0:
            pos = min(tgt, max(start, raw))
        else:
            pos = max(tgt, min(start, raw))
        # A completed full-travel run hits the physical limit -> snap (re-calibrate).
        if self._full_end and seconds > 0 and elapsed >= seconds * 0.9:
            pos = tgt
        return max(0.0, min(100.0, pos))

    @property
    def current_cover_position(self) -> int | None:
        if self._moving:
            live = self._estimate(monotonic() - self._move_started, self._move_seconds)
            return int(round(live))
        return int(round(self._position)) if self._position is not None else None

    @property
    def is_closed(self) -> bool | None:
        p = self.current_cover_position
        return None if p is None else p <= 0

    @property
    def is_opening(self) -> bool:
        return self._moving == "opening"

    @property
    def is_closing(self) -> bool:
        return self._moving == "closing"

    async def async_open_cover(self, **kwargs) -> None:
        await self._go(100)

    async def async_close_cover(self, **kwargs) -> None:
        await self._go(0)

    async def async_set_cover_position(self, **kwargs) -> None:
        await self._go(int(kwargs[ATTR_POSITION]))

    async def async_stop_cover(self, **kwargs) -> None:
        self.coordinator.client.async_stop(self._key)
        await self._cancel_move()

    async def _cancel_move(self) -> None:
        """Stop any in-flight movement and let it finalize the estimated position."""
        if self._run_task is not None and not self._run_task.done():
            self.coordinator.client.async_stop(self._key)
            try:
                await self._run_task
            except Exception:  # noqa: BLE001
                pass

    async def _go(self, target: int) -> None:
        target = max(0, min(100, int(target)))
        await self._cancel_move()
        start = self._position if self._position is not None else 0.0
        if target > start:
            direction, moving, sign, opening = MOVE_FORWARD, "opening", 1.0, True
        elif target < start:
            direction, moving, sign, opening = MOVE_REVERSE, "closing", -1.0, False
        else:
            return  # already at the requested position

        full = self.coordinator.get_travel(self._key, opening)  # open vs close timed separately
        seconds = abs(target - start) / 100.0 * full
        full_end = target in (0, 100)
        # Closing fully drives until the current stall finds the hard stop. Opening uses stall
        # as an over-travel SAFEGUARD but stops at the timed target, so it lands where you set
        # it (Open Travel Time) rather than at the mechanical limit.
        detect_stall = self._stall_capable and (opening or target == 0)
        if target == 0 and self._stall_capable:
            seconds = max(seconds, full * 1.5)  # generous ceiling; the stall is the real stop
        elif full_end and not detect_stall:
            seconds += full * 0.2  # slides / non-stall: time-based overshoot to hit the limit
        seconds = min(seconds, float(COVER_MAX_MOVE_SECONDS))

        self._moving = moving
        self._target = float(target)
        self._move_dir_sign = sign
        self._move_start_pos = start
        self._move_started = monotonic()
        self._move_seconds = seconds
        self._move_full = full
        self._full_end = full_end
        self._detect_stall = detect_stall
        self._write_state()
        self._run_task = self.hass.async_create_task(self._run(direction, seconds))

    async def _run(self, direction: int, seconds: float) -> None:
        self._ticker = self.hass.async_create_task(self._tick())
        stall_amps = self.coordinator.get_stall_amps(self._key) if self._detect_stall else 0.0
        elapsed, stalled = 0.0, False
        try:
            elapsed, stalled = await self.coordinator.client.async_move(
                self._key, direction, seconds, max_seconds=COVER_MAX_MOVE_SECONDS,
                stall_amps=stall_amps, stall_hold=COVER_STALL_HOLD_SECONDS,
                stall_arm=COVER_STALL_ARM_SECONDS,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Compass %s move failed: %s", self._attr_name, err)
        finally:
            if self._ticker is not None:
                self._ticker.cancel()
                self._ticker = None
            if stalled:
                # Hit the physical limit -> fully open (forward) or fully closed (reverse).
                self._position = 100.0 if direction == MOVE_FORWARD else 0.0
            else:
                self._position = self._estimate(elapsed, seconds)
            self._moving = None
            self._target = None
            self._write_state()
            await self.coordinator.async_request_refresh()

    async def _tick(self) -> None:
        """Refresh the estimated position in the UI while the motor runs."""
        try:
            while True:
                await asyncio.sleep(1.5)
                self._write_state()
        except asyncio.CancelledError:
            pass


class CompassVentLid(CoordinatorEntity[CompassCoordinator], CoverEntity):
    """The powered lid of a roof fan. It reports its real position (0/25/50/75/100 %)."""

    _attr_has_entity_name = False
    _attr_device_class = CoverDeviceClass.DAMPER

    def __init__(self, coordinator: CompassCoordinator, device) -> None:
        super().__init__(coordinator)
        self._key = device.key
        self._quarter = bool(device.caps & 0x10)
        self._attr_unique_id = f"{coordinator.address}_vent_{device.key}"
        self._attr_name = f"{device.name} Lid"
        features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
        if self._quarter:
            features |= CoverEntityFeature.SET_POSITION
        self._attr_supported_features = features
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            name="Compass Console",
            manufacturer="Lippert / IDS",
            model="OneControl IDS-CAN gateway",
        )

    @property
    def _vent(self):
        dev = (self.coordinator.data or {}).get(self._key)
        d = dev.data if dev else None
        return d["vent"] if d else None

    @property
    def available(self) -> bool:
        return super().available and self._vent is not None

    @property
    def current_cover_position(self) -> int | None:
        return self._vent

    @property
    def is_closed(self) -> bool | None:
        v = self._vent
        return None if v is None else v == 0

    async def _go(self, position: int) -> None:
        cmd = P.fan_vent_cmd(position)
        want = cmd[1][0]
        try:
            await self.coordinator.client.async_command(
                self._key, cmd, lambda dev: bool(dev.data) and dev.data["vent"] == want, 5.0)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Compass %s lid command failed: %s", self.name, err)
        await self.coordinator.async_request_refresh()

    async def async_open_cover(self, **kwargs) -> None:
        await self._go(100)

    async def async_close_cover(self, **kwargs) -> None:
        await self._go(0)

    async def async_set_cover_position(self, **kwargs) -> None:
        await self._go(int(kwargs[ATTR_POSITION]))
