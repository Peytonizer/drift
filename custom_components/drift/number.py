"""Comfort band numbers."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.entity import EntityCategory

from .entity import DriftEntity

BANDS = [
    ("tier1_min", "Tier 1 overnight min"),
    ("tier1_max", "Tier 1 overnight max"),
    ("tier2_min", "Tier 2 overnight min"),
    ("tier2_max", "Tier 2 overnight max"),
    ("tier3_min", "Tier 3 overnight min"),
    ("tier3_max", "Tier 3 overnight max"),
    ("day_min", "Daytime guard min"),
    ("day_max", "Daytime guard max"),
]


async def async_setup_entry(hass, entry, async_add_entities):
    c = entry.runtime_data
    async_add_entities([BandNumber(c, k, n) for k, n in BANDS])


class BandNumber(DriftEntity, NumberEntity):
    _attr_native_min_value = 12
    _attr_native_max_value = 32
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:thermometer-lines"

    def __init__(self, c, key, name):
        super().__init__(c, key, name)
        self._key = key

    @property
    def native_value(self):
        return self.controller.bands[self._key]

    async def async_set_native_value(self, value: float) -> None:
        other = self._key.replace("_min", "_max") if self._key.endswith("_min") else self._key.replace("_max", "_min")
        o = self.controller.bands[other]
        if (self._key.endswith("_min") and value > o - 1) or (self._key.endswith("_max") and value < o + 1):
            raise ValueError("Band min must be at least 1°C below max")
        await self.controller.async_set_band(self._key, value)
