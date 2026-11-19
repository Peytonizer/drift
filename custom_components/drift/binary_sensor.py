"""Overnight window active."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.util import dt as dt_util

from .entity import DriftEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([WindowActive(entry.runtime_data)])


class WindowActive(DriftEntity, BinarySensorEntity):
    _attr_icon = "mdi:weather-night"

    def __init__(self, c):
        super().__init__(c, "in_window", "Overnight window active")

    @property
    def is_on(self):
        return self.controller.in_window(dt_util.now())
