"""Tier-1 sleepwear TOG selector."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity

from .entity import DriftEntity
from .tog import TOG_LEVELS, TOG_MANUAL


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([TogSelect(entry.runtime_data)])


class TogSelect(DriftEntity, SelectEntity):
    _attr_icon = "mdi:tshirt-crew"
    _attr_options = [TOG_MANUAL, *TOG_LEVELS]

    def __init__(self, c):
        super().__init__(c, "tier1_tog", "Tier 1 sleepwear TOG")

    @property
    def current_option(self):
        return self.controller.tog

    @property
    def extra_state_attributes(self):
        c = self.controller
        return {
            "band_from_chart": None if c.tog == TOG_MANUAL else c.tog_band(c.tog),
            "chart": {t: c.tog_band(t) for t in TOG_LEVELS},
        }

    async def async_select_option(self, option: str) -> None:
        await self.controller.async_set_tog(option)
