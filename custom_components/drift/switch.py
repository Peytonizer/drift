"""Switches: master enable and solar pre-cool."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity

from .entity import DriftEntity


async def async_setup_entry(hass, entry, async_add_entities):
    c = entry.runtime_data
    async_add_entities([EnabledSwitch(c), PrecoolSwitch(c)])


class EnabledSwitch(DriftEntity, SwitchEntity):
    _attr_icon = "mdi:robot"

    def __init__(self, c):
        super().__init__(c, "enabled", "Control enabled")

    @property
    def is_on(self):
        return self.controller.enabled

    async def async_turn_on(self, **kwargs):
        await self.controller.async_set_enabled(True)

    async def async_turn_off(self, **kwargs):
        await self.controller.async_set_enabled(False)


class PrecoolSwitch(DriftEntity, SwitchEntity):
    _attr_icon = "mdi:solar-power"

    def __init__(self, c):
        super().__init__(c, "precool", "Solar pre-cool")

    @property
    def is_on(self):
        return self.controller.precool_enabled

    async def async_turn_on(self, **kwargs):
        await self.controller.async_set_precool(True)

    async def async_turn_off(self, **kwargs):
        await self.controller.async_set_precool(False)
