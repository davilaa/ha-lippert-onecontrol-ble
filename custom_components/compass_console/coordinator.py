"""DataUpdateCoordinator for the Compass Console integration.

Every update cycle opens one short BLE session, unlocks, listens to the periodic
IDS-CAN device broadcasts, and returns a {func_id: Device} map. All BLE access is
serialized inside CompassClient (the console allows a single connection).
"""
import logging
from datetime import timedelta

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .compass_ble import CompassClient
from .const import COVER_FULL_TRAVEL_SECONDS, COVER_STALL_AMPS, DOMAIN

_LOGGER = logging.getLogger(__name__)


class CompassCoordinator(DataUpdateCoordinator):
    """Polls the console for the current device/state map (keyed by function id)."""

    def __init__(self, hass: HomeAssistant, address: str, scan_interval: int) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.address = address
        # Live cover configuration (set from the config entry / Options flow). cover_travel
        # maps cover_key -> {"open": secs, "close": secs} (a bare number = legacy: both dirs).
        self.default_travel: float = COVER_FULL_TRAVEL_SECONDS
        self.cover_travel: dict = {}
        self.default_stall: float = COVER_STALL_AMPS
        self.cover_stall: dict = {}  # cover_key -> stall-current threshold (amps)
        self.covers: dict = {}  # cover_key -> CompassCover entity (for live re-calibration)

        async def _get_device(addr: str):
            # The console needs a bonded (encrypted) BLE link, which only works through a
            # LOCAL BlueZ adapter. ESPHome Bluetooth proxies can't do bonded pairing and
            # would fail the KeySeed unlock, so prefer a local adapter over any remote
            # (proxy) scanner even when the proxy has a stronger signal.
            try:
                from habluetooth import BaseHaRemoteScanner

                devs = bluetooth.async_scanner_devices_by_address(hass, addr, connectable=True)
                local = [d for d in devs if not isinstance(d.scanner, BaseHaRemoteScanner)]
                if local:
                    return local[0].ble_device
                if devs:
                    return devs[0].ble_device
            except Exception:  # noqa: BLE001
                pass
            return bluetooth.async_ble_device_from_address(hass, addr, connectable=True)

        self.client = CompassClient(address, ble_device_getter=_get_device)

    def get_travel(self, key: str, opening: bool) -> float:
        """Full-travel time (s) for a cover in the given direction; per-cover override or default."""
        entry = self.cover_travel.get(key)
        try:
            if isinstance(entry, dict):
                val = entry.get("open" if opening else "close")
                if val is not None:
                    return float(val)
            elif isinstance(entry, (int, float)):  # legacy single value -> both directions
                return float(entry)
        except (TypeError, ValueError):
            pass
        return float(self.default_travel)

    def get_stall_amps(self, key: str) -> float:
        """Stall-current threshold (amps) for a cover; per-cover override or default."""
        try:
            return float(self.cover_stall.get(key, self.default_stall))
        except (TypeError, ValueError):
            return float(self.default_stall)

    async def _async_update_data(self):
        try:
            devmap = await self.client.async_enumerate()
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"Compass Console poll failed: {err}") from err
        # Key by stable device identity (func+instances). CAN addresses change on
        # every console power-cycle; func+instance does not, and it disambiguates
        # multiple devices that share a function (e.g. two grey tanks).
        return {dev.key: dev for dev in devmap.values()}
