"""The Compass Console (Lippert OneControl) integration."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ADDRESS,
    CONF_SCAN_INTERVAL,
    COVER_STALL_OPT,
    COVER_TRAVEL_OPT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    RELEASED_OPT,
)
from .coordinator import CompassCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.COVER,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.LIGHT,
    Platform.CLIMATE,
    Platform.FAN,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    address = entry.data[CONF_ADDRESS]
    scan_interval = entry.options.get(
        CONF_SCAN_INTERVAL,
        entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
    )

    coordinator = CompassCoordinator(hass, address, scan_interval)
    coordinator.cover_travel = dict(entry.options.get(COVER_TRAVEL_OPT, {}))
    coordinator.cover_stall = dict(entry.options.get(COVER_STALL_OPT, {}))
    # Restore a persisted "released for the phone app" state so a hand-off survives a restart:
    # if released, the first fetch below won't grab the console back.
    coordinator.client.set_released_initial(entry.options.get(RELEASED_OPT, False))
    # Do NOT block setup on the first fetch: if the console is unpaired/unreachable we
    # still want the platforms (especially the Pair button) to load so the user can
    # recover. Entities populate dynamically once a poll succeeds.
    await coordinator.async_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply Options changes live (no reload -> covers keep their estimated positions)."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is None:
        return
    coordinator.cover_travel = dict(entry.options.get(COVER_TRAVEL_OPT, {}))
    coordinator.cover_stall = dict(entry.options.get(COVER_STALL_OPT, {}))
    scan = entry.options.get(
        CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )
    try:
        coordinator.update_interval = timedelta(seconds=int(scan))
    except (TypeError, ValueError):
        pass


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
        if coordinator is not None:
            await coordinator.client.async_close()
    return unloaded
