"""Config + options flow for Drift."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import selector as sel

from .tog import DEFAULT_CHART
from .const import (
    CONF_AC_PHASE_POWER,
    CONF_ALLOW_COOLING,
    CONF_ALLOW_HEATING,
    CONF_AUTOMATIONS_TO_DISABLE,
    CONF_COOL_DEPTH,
    CONF_FEED_IN_TARIFF,
    CONF_GRID_IMPORT_NEGATIVE,
    CONF_GRID_POWER,
    CONF_HEAT_DEPTH,
    CONF_HORIZON,
    CONF_IMPORT_TARIFF,
    CONF_MAIN_CLIMATE,
    CONF_MIN_OFF,
    CONF_MIN_OPEN_ZONES,
    CONF_MIN_RUN,
    CONF_MYZONE_SELECT,
    CONF_NAME,
    CONF_NOTIFY_SERVICE,
    CONF_NOTIFY_TIME,
    CONF_OVERRIDE_MINUTES,
    CONF_PRECOOL_DEPTH,
    CONF_PRECOOL_MIN_OUTDOOR,
    CONF_PRECOOL_MIN_SURPLUS,
    CONF_PRECOOL_START_HOUR,
    CONF_RECOVERY_MAX,
    CONF_RECOVERY_MINUTES,
    CONF_START_LAG,
    CONF_TIER1_SENSOR,
    CONF_OUTDOOR_SENSOR,
    CONF_TIER1_ZONE,
    CONF_TIER2_ZONES,
    CONF_TIER3_ZONES,
    CONF_CONSTANT_ZONES,
    CONF_TOLERANCE,
    CONF_WEATHER,
    DEFAULTS,
    DOMAIN,
)


def _ent(domain: str, multiple: bool = False, device_class: str | None = None) -> sel.EntitySelector:
    cfg = sel.EntitySelectorConfig(domain=domain, multiple=multiple)
    if device_class:
        cfg = sel.EntitySelectorConfig(domain=domain, multiple=multiple, device_class=device_class)
    return sel.EntitySelector(cfg)


def _num(lo: float, hi: float, step: float, unit: str | None = None) -> sel.NumberSelector:
    kw = {"unit_of_measurement": unit} if unit else {}
    return sel.NumberSelector(sel.NumberSelectorConfig(min=lo, max=hi, step=step, mode=sel.NumberSelectorMode.BOX, **kw))


def _opt(key: str, src: dict) -> Any:
    """vol.Optional with a suggested value only when one exists (entity selectors reject None)."""
    v = src.get(key)
    return vol.Optional(key, description={"suggested_value": v}) if v not in (None, "", []) else vol.Optional(key)


def setup_schema(d: dict) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=d.get(CONF_NAME, "Drift")): str,
            vol.Required(CONF_MAIN_CLIMATE, default=d.get(CONF_MAIN_CLIMATE, vol.UNDEFINED)): _ent("climate"),
            _opt(CONF_MYZONE_SELECT, d): _ent("select"),
            vol.Required(CONF_WEATHER, default=d.get(CONF_WEATHER, vol.UNDEFINED)): _ent("weather"),
        }
    )


def zones_schema(d: dict) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_TIER1_ZONE, default=d.get(CONF_TIER1_ZONE, vol.UNDEFINED)): _ent("climate"),
            _opt(CONF_TIER1_SENSOR, d): _ent("sensor", device_class="temperature"),
            _opt(CONF_OUTDOOR_SENSOR, d): _ent("sensor", device_class="temperature"),
            vol.Optional(CONF_TIER2_ZONES, default=d.get(CONF_TIER2_ZONES, [])): _ent("climate", multiple=True),
            vol.Optional(CONF_TIER3_ZONES, default=d.get(CONF_TIER3_ZONES, [])): _ent("climate", multiple=True),
            vol.Optional(CONF_CONSTANT_ZONES, default=d.get(CONF_CONSTANT_ZONES, [])): _ent("climate", multiple=True),
        }
    )


def energy_schema(d: dict) -> vol.Schema:
    return vol.Schema(
        {
            _opt(CONF_GRID_POWER, d): _ent("sensor"),
            vol.Optional(CONF_GRID_IMPORT_NEGATIVE, default=d.get(CONF_GRID_IMPORT_NEGATIVE, True)): bool,
            _opt(CONF_AC_PHASE_POWER, d): _ent("sensor"),
            vol.Optional(CONF_AUTOMATIONS_TO_DISABLE, default=d.get(CONF_AUTOMATIONS_TO_DISABLE, [])): _ent(
                "automation", multiple=True
            ),
        }
    )


def tuning_schema(d: dict) -> vol.Schema:
    g = lambda k: d.get(k, DEFAULTS[k])  # noqa: E731
    return vol.Schema(
        {
            vol.Required(CONF_MIN_RUN, default=g(CONF_MIN_RUN)): _num(10, 120, 5, "min"),
            vol.Required(CONF_MIN_OFF, default=g(CONF_MIN_OFF)): _num(5, 120, 5, "min"),
            vol.Required(CONF_START_LAG, default=g(CONF_START_LAG)): _num(0, 60, 5, "min"),
            vol.Required(CONF_COOL_DEPTH, default=g(CONF_COOL_DEPTH)): _num(0.5, 4, 0.5, "°C"),
            vol.Required(CONF_HEAT_DEPTH, default=g(CONF_HEAT_DEPTH)): _num(0.5, 4, 0.5, "°C"),
            vol.Required(CONF_TOLERANCE, default=g(CONF_TOLERANCE)): _num(0, 1, 0.1, "°C"),
            vol.Required(CONF_RECOVERY_MAX, default=g(CONF_RECOVERY_MAX)): _num(0, 1.5, 0.1, "°C"),
            vol.Required(CONF_RECOVERY_MINUTES, default=g(CONF_RECOVERY_MINUTES)): _num(0, 180, 5, "min"),
            vol.Required(CONF_HORIZON, default=g(CONF_HORIZON)): _num(2, 24, 1, "h"),
            vol.Required(CONF_MIN_OPEN_ZONES, default=g(CONF_MIN_OPEN_ZONES)): _num(1, 10, 1),
            vol.Required(CONF_OVERRIDE_MINUTES, default=g(CONF_OVERRIDE_MINUTES)): _num(15, 720, 15, "min"),
            vol.Required(CONF_ALLOW_COOLING, default=g(CONF_ALLOW_COOLING)): bool,
            vol.Required(CONF_ALLOW_HEATING, default=g(CONF_ALLOW_HEATING)): bool,
            vol.Required(CONF_PRECOOL_DEPTH, default=g(CONF_PRECOOL_DEPTH)): _num(0.5, 5, 0.5, "°C"),
            vol.Required(CONF_PRECOOL_START_HOUR, default=g(CONF_PRECOOL_START_HOUR)): _num(9, 18, 1, "h"),
            vol.Required(CONF_PRECOOL_MIN_OUTDOOR, default=g(CONF_PRECOOL_MIN_OUTDOOR)): _num(20, 45, 1, "°C"),
            vol.Required(CONF_PRECOOL_MIN_SURPLUS, default=g(CONF_PRECOOL_MIN_SURPLUS)): _num(500, 15000, 250, "W"),
            vol.Required(CONF_IMPORT_TARIFF, default=g(CONF_IMPORT_TARIFF)): _num(0, 2, 0.001, "$/kWh"),
            vol.Required(CONF_FEED_IN_TARIFF, default=g(CONF_FEED_IN_TARIFF)): _num(0, 1, 0.001, "$/kWh"),
        }
    )


def tog_schema(d: dict) -> vol.Schema:
    fields: dict = {
        _opt(CONF_NOTIFY_SERVICE, d): sel.TextSelector(),
        vol.Required(CONF_NOTIFY_TIME, default=d.get(CONF_NOTIFY_TIME, DEFAULTS[CONF_NOTIFY_TIME])): sel.TimeSelector(),
    }
    for tog, (lo, hi) in DEFAULT_CHART.items():
        k = tog.replace(".", "_")
        fields[vol.Required(f"tog_{k}_min", default=d.get(f"tog_{k}_min", lo))] = _num(8, 32, 0.5, "°C")
        fields[vol.Required(f"tog_{k}_max", default=d.get(f"tog_{k}_max", hi))] = _num(8, 32, 0.5, "°C")
    return vol.Schema(fields)


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "")}


class DriftConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def _guess(self) -> dict:
        """Pre-fill for Advantage Air style setups."""
        g: dict[str, Any] = {}
        climates = sorted(s.entity_id for s in self.hass.states.async_all("climate"))
        zone_like = [c for c in climates if c.count("_") >= 1 and c.split(".")[1].startswith("ac_")]
        if "climate.ac" in climates:
            g[CONF_MAIN_CLIMATE] = "climate.ac"
        selects = [s.entity_id for s in self.hass.states.async_all("select") if s.entity_id.endswith("myzone")]
        if selects:
            g[CONF_MYZONE_SELECT] = selects[0]
        weathers = [s.entity_id for s in self.hass.states.async_all("weather")]
        if weathers:
            g[CONF_WEATHER] = weathers[0]
        if zone_like:
            g["_zones"] = zone_like
        return g

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(_clean(user_input))
            return await self.async_step_zones()
        return self.async_show_form(step_id="user", data_schema=setup_schema(self._guess()))

    async def async_step_zones(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            if user_input[CONF_TIER1_ZONE] == self._data.get(CONF_MAIN_CLIMATE):
                errors[CONF_TIER1_ZONE] = "zone_is_main"
            else:
                self._data.update(_clean(user_input))
                return await self.async_step_energy()
        return self.async_show_form(step_id="zones", data_schema=zones_schema(self._data), errors=errors)

    async def async_step_energy(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(_clean(user_input))
            await self.async_set_unique_id(self._data[CONF_MAIN_CLIMATE])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=self._data[CONF_NAME], data=self._data)
        return self.async_show_form(step_id="energy", data_schema=energy_schema(self._data))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return DriftOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {"comfort_rule": ComfortRuleFlow}


class DriftOptionsFlow(OptionsFlow):
    def __init__(self) -> None:
        self._opts: dict[str, Any] = {}

    @property
    def _cur(self) -> dict:
        return {**self.config_entry.data, **self.config_entry.options, **self._opts}

    async def async_step_init(self, user_input: dict | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["zones", "energy", "tuning", "tog"])

    async def async_step_zones(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._opts.update(user_input)
            # a cleared optional selector is omitted, so clear the stored value explicitly
            for k in (CONF_TIER1_SENSOR, CONF_OUTDOOR_SENSOR):
                if k not in user_input:
                    self._opts[k] = None
            return self._save()
        return self.async_show_form(step_id="zones", data_schema=zones_schema(self._cur))

    async def async_step_energy(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._opts.update(user_input)
            for k in (CONF_GRID_POWER, CONF_AC_PHASE_POWER):
                if k not in user_input:
                    self._opts[k] = None
            return self._save()
        return self.async_show_form(step_id="energy", data_schema=energy_schema(self._cur))

    async def async_step_tuning(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._opts.update(user_input)
            return self._save()
        return self.async_show_form(step_id="tuning", data_schema=tuning_schema(self._cur))

    async def async_step_tog(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            for tog in DEFAULT_CHART:
                k = tog.replace(".", "_")
                if user_input[f"tog_{k}_min"] >= user_input[f"tog_{k}_max"]:
                    errors[f"tog_{k}_min"] = "min_above_max"
            if not errors:
                self._opts.update(user_input)
                if CONF_NOTIFY_SERVICE not in user_input:
                    self._opts[CONF_NOTIFY_SERVICE] = None
                return self._save()
        return self.async_show_form(step_id="tog", data_schema=tog_schema(self._cur), errors=errors)

    def _save(self) -> ConfigFlowResult:
        return self.async_create_entry(data={**self.config_entry.options, **self._opts})


WEEKDAY_KEYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _select(options: list[str], key: str, multiple: bool = False) -> sel.SelectSelector:
    return sel.SelectSelector(
        sel.SelectSelectorConfig(
            options=options, translation_key=key, multiple=multiple, mode=sel.SelectSelectorMode.DROPDOWN
        )
    )


def rule_schema(zone_ids: list[str]) -> vol.Schema:
    zones_sel = sel.EntitySelector(sel.EntitySelectorConfig(include_entities=zone_ids, multiple=True))
    return vol.Schema(
        {
            vol.Required("name"): str,
            vol.Required("zones"): zones_sel,
            vol.Required("trigger", default="schedule"): _select(["schedule", "presence"], "rule_trigger"),
            vol.Optional("schedule"): _ent("schedule"),
            vol.Optional("presence"): _ent("binary_sensor", multiple=True),
            vol.Optional("presence_days", default=["saturday", "sunday"]): _select(WEEKDAY_KEYS, "weekday", multiple=True),
            vol.Optional("dwell_minutes", default=5): _num(0, 60, 1, "min"),
            vol.Optional("hold_minutes", default=30): _num(0, 240, 5, "min"),
            vol.Required("band", default="fixed"): _select(["fixed", "tier1"], "rule_band"),
            vol.Optional("min", default=21.0): _num(12, 30, 0.5, "°C"),
            vol.Optional("max", default=25.0): _num(14, 32, 0.5, "°C"),
        }
    )


class ComfortRuleFlow(ConfigSubentryFlow):
    """Add or edit one daytime comfort rule."""

    def _zone_ids(self) -> list[str]:
        entry = self._get_entry()
        cfg = {**entry.data, **entry.options}
        ids = [cfg.get(CONF_TIER1_ZONE)] + list(cfg.get(CONF_TIER2_ZONES) or []) + list(cfg.get(CONF_TIER3_ZONES) or [])
        return [i for i in ids if i]

    def _validate(self, data: dict) -> dict[str, str]:
        errors = {}
        if not data.get("zones"):
            errors["zones"] = "no_zones"
        if data["trigger"] == "schedule" and not data.get("schedule"):
            errors["schedule"] = "schedule_required"
        if data["trigger"] == "presence":
            if not data.get("presence"):
                errors["presence"] = "presence_required"
            if not data.get("presence_days"):
                errors["presence_days"] = "days_required"
        if data["band"] == "fixed" and float(data.get("min", 0)) > float(data.get("max", 0)) - 1:
            errors["min"] = "band_too_narrow"
        return errors

    async def _form(self, step: str, user_input: dict | None, current: dict) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._validate(user_input)
            if not errors:
                data = {k: v for k, v in user_input.items() if v not in (None, "", [])}
                if step == "user":
                    return self.async_create_entry(title=data["name"], data=data)
                return self.async_update_and_abort(
                    self._get_entry(), self._get_reconfigure_subentry(), title=data["name"], data=data
                )
            current = user_input
        schema = self.add_suggested_values_to_schema(rule_schema(self._zone_ids()), current)
        return self.async_show_form(step_id=step, data_schema=schema, errors=errors)

    async def async_step_user(self, user_input: dict | None = None) -> SubentryFlowResult:
        return await self._form("user", user_input, {})

    async def async_step_reconfigure(self, user_input: dict | None = None) -> SubentryFlowResult:
        return await self._form("reconfigure", user_input, dict(self._get_reconfigure_subentry().data))
