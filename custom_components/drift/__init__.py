"""Drift: forecast-aware, tiered climate control for zoned ducted AC."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import PLATFORMS
from .controller import DriftController

type DriftConfigEntry = ConfigEntry[DriftController]


async def async_setup_entry(hass: HomeAssistant, entry: DriftConfigEntry) -> bool:
    controller = DriftController(hass, entry)
    entry.runtime_data = controller
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await controller.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DriftConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_stop()
    return ok


async def _async_reload(hass: HomeAssistant, entry: DriftConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
