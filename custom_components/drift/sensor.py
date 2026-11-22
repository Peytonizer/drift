"""Sensors: status, predictions, AC energy, nightly review, model."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower, UnitOfTemperature
from homeassistant.helpers.entity import EntityCategory
from homeassistant.util import dt as dt_util

from .entity import DriftEntity


async def async_setup_entry(hass, entry, async_add_entities):
    c = entry.runtime_data
    async_add_entities(
        [
            StatusSensor(c),
            PredictionSensor(c, 60, "Tier 1 predicted in 1h"),
            PredictionSensor(c, 180, "Tier 1 predicted in 3h"),
            AcPowerSensor(c),
            EnergySensor(c, "ac_kwh", "AC energy"),
            EnergySensor(c, "solar_kwh", "AC energy from solar"),
            EnergySensor(c, "grid_kwh", "AC energy from grid"),
            LastNightSensor(c),
            ReviewSensor(c),
            ModelSensor(c),
            TogRecommendationSensor(c),
        ]
    )


def _door_label(closed: bool | None) -> str:
    return "unknown" if closed is None else ("closed" if closed else "open")


class StatusSensor(DriftEntity, SensorEntity):
    _attr_icon = "mdi:home-thermometer"

    def __init__(self, c):
        super().__init__(c, "status", "Status")

    @property
    def native_value(self):
        return self.controller.status

    @property
    def extra_state_attributes(self):
        c = self.controller
        d = c.decision
        return {
            "reason": c.reason,
            "enabled": c.enabled,
            "planned_action": d.action if d else None,
            "precool": d.precool if d else False,
            "myzone": d.myzone if d else None,
            "next_start": d.next_start.isoformat() if d and d.next_start else None,
            "override_until": c.override_until.isoformat() if c.override_until else None,
            "ac_reachable": c.ac_unavailable_since is None and c.fault is None,
            "ac_problem": c.fault,
            "pending_command": (
                {"mode": c.pending["mode"], "since": c.pending["first"].isoformat(), "attempts": c.pending["attempts"]}
                if c.pending
                else None
            ),
            "in_window": c.in_window(dt_util.now()),
            "active_rules": c.active_rules(dt_util.now()),
            "outdoor_now": c.outdoor,
            "outdoor_source": c.outdoor_source,
            "zones": {
                z.name: {
                    "tier": z.tier,
                    "temp": c.zone_temp(z),
                    "band": c.band(z, dt_util.now()),
                    **({"door": _door_label(c.door_closed(z))} if z.door_sensor_id else {}),
                    "command": (
                        {
                            "on": d.zone_commands[z.climate_id].on,
                            "setpoint": d.zone_commands[z.climate_id].setpoint,
                            "target": d.zone_commands[z.climate_id].target,
                        }
                        if d and z.climate_id in d.zone_commands
                        else None
                    ),
                }
                for z in c.zones
            },
            "needs": [
                {"zone": n.zone_id, "mode": n.mode, "latest_start": n.latest_start.isoformat(), "violation_at": n.violation_at.isoformat(), "peak_excess": round(n.peak_excess, 2)}
                for n in (d.needs if d else [])
            ][:6],
        }


class PredictionSensor(DriftEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, c, minutes, name):
        super().__init__(c, f"tier1_pred_{minutes}", name)
        self._minutes = minutes

    @property
    def native_value(self):
        return self.controller.tier1_prediction(self._minutes)

    @property
    def extra_state_attributes(self):
        if self._minutes != 60:
            return None
        c = self.controller
        t1 = next((z for z in c.zones if z.tier == 1), None)
        if not (c.decision and t1):
            return None
        traj = c.decision.predictions.get(t1.climate_id) or []
        return {
            "assumes": "AC stays off",
            "trajectory": [{"time": w.strftime("%H:%M"), "temp": round(v, 2)} for w, v in traj[::6]],
            "outdoor_forecast": c.outdoor_forecast_summary(),
        }


class AcPowerSensor(DriftEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, c):
        super().__init__(c, "ac_power", "AC power (estimated)")

    @property
    def native_value(self):
        return round(self.controller.ac_watts_now)

    @property
    def extra_state_attributes(self):
        c = self.controller
        return {
            "learned_running_watts": round(c.ac_power.ac_watts),
            "learned_from_transitions": c.ac_power.samples_learned,
            "recent_steps_w": [round(x) for x in c.ac_power.steps[-5:]],
            "solar_share_now_w": round(c.solar_w_now),
        }


class EnergySensor(DriftEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 2

    def __init__(self, c, field, name):
        super().__init__(c, field, name)
        self._field = field

    @property
    def native_value(self):
        return round(getattr(self.controller.totals, self._field), 3)


class LastNightSensor(DriftEntity, SensorEntity):
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:weather-night"

    def __init__(self, c):
        super().__init__(c, "last_night", "Last night tier 1 in band")

    @property
    def native_value(self):
        n = self.controller.nights
        return n[-1].get("tier1_in_band_pct") if n else None

    @property
    def extra_state_attributes(self):
        n = self.controller.nights
        return n[-1] if n else {"note": "First review appears after the first full overnight window"}


class ReviewSensor(DriftEntity, SensorEntity):
    _attr_icon = "mdi:clipboard-text-search"

    def __init__(self, c):
        super().__init__(c, "review", "Tuning suggestions")

    @property
    def native_value(self):
        return len(self.controller.suggestions)

    @property
    def extra_state_attributes(self):
        c = self.controller
        recent = c.nights[-7:]
        agg = {}
        if recent:
            agg = {
                "nights": len(recent),
                "avg_tier1_in_band_pct": round(sum((x.get("tier1_in_band_pct") or 0) for x in recent) / len(recent), 1),
                "avg_starts": round(sum(x["compressor_starts"] for x in recent) / len(recent), 1),
                "total_ac_kwh": round(sum(x["ac_kwh"] for x in recent), 2),
                "total_grid_kwh": round(sum(x["grid_kwh"] for x in recent), 2),
                "total_solar_kwh": round(sum(x["solar_kwh"] for x in recent), 2),
            }
        return {"suggestions": c.suggestions, "last_7_nights": agg, "history": recent}


class ModelSensor(DriftEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:brain"

    def __init__(self, c):
        super().__init__(c, "model", "Thermal model samples")

    @property
    def native_value(self):
        return sum(m.n_passive for m in self.controller.models.values())

    @property
    def extra_state_attributes(self):
        c = self.controller
        return {
            "trained_from_history": c.bootstrap_info,
            **{
            z.name: {
                k: (round(v, 4) if isinstance(v, float) else v)
                for k, v in c.models[z.climate_id].to_dict().items()
                if k != "P"
            }
            for z in c.zones
            },
            **{
                f"{z.name} door": {
                    state: {"samples": dm.n_passive, **{k: round(v, 4) for k, v in dm.to_dict().items() if k in ("a", "b", "c")}}
                    for state, dm in (("open", c.door_models[z.climate_id].open), ("closed", c.door_models[z.climate_id].closed))
                }
                for z in c.zones
                if z.climate_id in c.door_models
            },
        }


class TogRecommendationSensor(DriftEntity, SensorEntity):
    _attr_icon = "mdi:tshirt-crew-outline"

    def __init__(self, c):
        super().__init__(c, "tog_recommendation", "Tier 1 sleepwear recommendation")

    @property
    def native_value(self):
        r = self.controller.tog_rec
        return r["tog"] if r else None

    @property
    def extra_state_attributes(self):
        r = self.controller.tog_rec
        if not r:
            return {"note": "Needs a forecast and a tier 1 temperature"}
        lo, hi = self.controller.tog_band(r["tog"])
        return {**r, "band_if_selected": [lo, hi]}
