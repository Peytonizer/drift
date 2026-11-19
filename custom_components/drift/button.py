"""Relearn the room models from recorder history."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.entity import EntityCategory

from .entity import DriftEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([RelearnButton(entry.runtime_data)])


class RelearnButton(DriftEntity, ButtonEntity):
    _attr_icon = "mdi:school"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, c):
        super().__init__(c, "relearn", "Relearn room models from history")

    async def async_press(self) -> None:
        await self.controller.async_relearn()
