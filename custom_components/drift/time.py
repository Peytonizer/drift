"""Overnight window start/end."""
from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.helpers.entity import EntityCategory

from .entity import DriftEntity


async def async_setup_entry(hass, entry, async_add_entities):
    c = entry.runtime_data
    async_add_entities(
        [WindowTime(c, "window_start", "Overnight window start"), WindowTime(c, "window_end", "Overnight window end")]
    )


class WindowTime(DriftEntity, TimeEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:clock-outline"

    def __init__(self, c, key, name):
        super().__init__(c, key, name)
        self._key = key

    @property
    def native_value(self) -> time:
        return self.controller.window[self._key]

    async def async_set_value(self, value: time) -> None:
        await self.controller.async_set_window(self._key, value)
