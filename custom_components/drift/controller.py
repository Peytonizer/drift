"""Home Assistant glue: reads states, learns, plans, actuates, records."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    BAND_DEFAULTS,
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
    CONF_NOTIFY_SERVICE,
    CONF_NOTIFY_TIME,
    CONF_OVERRIDE_MINUTES,
    CONF_PRECOOL_DEPTH,
    CONF_PRECOOL_MIN_OUTDOOR,
    CONF_PRECOOL_MIN_SURPLUS,
    CONF_PRECOOL_START_HOUR,
    CONF_START_LAG,
    CONF_TIER1_SENSOR,
    CONF_TIER1_ZONE,
    CONF_TIER2_ZONES,
    CONF_TIER3_ZONES,
    CONF_TOG_MARGIN,
    CONF_TOLERANCE,
    CONF_WEATHER,
    CONTROL_INTERVAL_MIN,
    DEFAULTS,
    SAMPLE_INTERVAL_SEC,
    STORAGE_KEY,
    STORAGE_VERSION,
    WINDOW_DEFAULTS,
)
from .energy import AcPowerLearner, EnergyTotals, split_solar_grid
from .model import ZoneModel, solar_factor
from .planner import Decision, ForecastPoint, PlanInput, Settings, ZoneInput, outdoor_at, plan, project_passive
from .review import NightRecorder, ZoneSample, suggestions, summarise
from .tog import TOG_MANUAL, band_for, chart_from_options, recommend

_LOGGER = logging.getLogger(__name__)
ACTIVE_MODES = ("cool", "heat")


def _f(state) -> float | None:
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None, ""):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def _parse_time(v: str) -> time:
    h, m = v.split(":")[:2]
    return time(int(h), int(m))


class Zone:
    def __init__(self, climate_id: str, tier: int, sensor_id: str | None, name: str) -> None:
        self.climate_id = climate_id
        self.tier = tier
        self.sensor_id = sensor_id
        self.name = name
        self.prev: tuple[datetime, float] | None = None


class DriftController:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.cfg: dict[str, Any] = {**DEFAULTS, **entry.data, **entry.options}
        self._store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}")
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[Callable[[], None]] = []

        self.zones: list[Zone] = []
        self._build_zones()

        # persisted
        self.enabled = False
        self.precool_enabled = True
        self.bands: dict[str, float] = dict(BAND_DEFAULTS)
        self.window: dict[str, time] = {k: _parse_time(v) for k, v in WINDOW_DEFAULTS.items()}
        self.models: dict[str, ZoneModel] = {z.climate_id: ZoneModel() for z in self.zones}
        self.ac_power = AcPowerLearner()
        self.totals = EnergyTotals()
        self.night = NightRecorder()
        self.nights: list[dict] = []
        self.suggestions: list[str] = []
        self.tog: str = TOG_MANUAL
        self._notified_date: str | None = None

        # runtime
        self.tog_rec: dict | None = None
        self._tog_rec_at: datetime | None = None
        self.decision: Decision | None = None
        self.status = "starting"
        self.reason = ""
        self.forecast: list[ForecastPoint] = []
        self._forecast_at: datetime | None = None
        self.outdoor: float | None = None
        self.cloud: float | None = None
        self.ac_mode_actual = "off"
        self.ac_since: datetime | None = None
        self.last_stop: datetime | None = None
        self.running_precool = False
        self.expected_mode: str | None = None
        self.last_cmd: datetime | None = None
        self.override_until: datetime | None = None
        self.ac_watts_now = 0.0
        self.solar_w_now = 0.0
        self.grid_w_export: float | None = None
        self._was_in_window = False
        self._pending_preds: list[tuple[datetime, float]] = []
        self._last_learn_mode = "off"
        self._save_counter = 0

    # ------------------------------------------------------------------ setup
    def _build_zones(self) -> None:
        def nm(eid: str) -> str:
            st = self.hass.states.get(eid)
            name = st.attributes.get("friendly_name", eid) if st else eid
            return name[3:] if name.startswith("AC ") else name

        t1 = self.cfg.get(CONF_TIER1_ZONE)
        if t1:
            self.zones.append(Zone(t1, 1, self.cfg.get(CONF_TIER1_SENSOR), nm(t1)))
        for eid in self.cfg.get(CONF_TIER2_ZONES, []) or []:
            if eid != t1:
                self.zones.append(Zone(eid, 2, None, nm(eid)))
        for eid in self.cfg.get(CONF_TIER3_ZONES, []) or []:
            if eid != t1 and eid not in (self.cfg.get(CONF_TIER2_ZONES) or []):
                self.zones.append(Zone(eid, 3, None, nm(eid)))

    async def async_start(self) -> None:
        data = await self._store.async_load() or {}
        self.enabled = data.get("enabled", False)
        self.precool_enabled = data.get("precool_enabled", True)
        self.bands.update(data.get("bands", {}))
        for k, v in (data.get("window") or {}).items():
            self.window[k] = _parse_time(v)
        for z in self.zones:
            self.models[z.climate_id] = ZoneModel.from_dict((data.get("models") or {}).get(z.climate_id))
        self.ac_power = AcPowerLearner.from_dict(data.get("ac_power"))
        self.totals = EnergyTotals.from_dict(data.get("totals"))
        self.night = NightRecorder.from_dict(data.get("night"))
        self.nights = data.get("nights", [])
        self.suggestions = data.get("suggestions", [])
        self._was_in_window = data.get("was_in_window", False)
        self.tog = data.get("tog", TOG_MANUAL)
        self._notified_date = data.get("notified_date")
        if self.tog != TOG_MANUAL:
            self.bands["tier1_min"], self.bands["tier1_max"] = self.tog_band(self.tog)  # chart may have been edited

        main = self.hass.states.get(self.cfg[CONF_MAIN_CLIMATE])
        self.ac_mode_actual = main.state if main and main.state in ACTIVE_MODES else "off"
        if self.ac_mode_actual != "off":
            self.ac_since = main.last_changed
        self.expected_mode = self.ac_mode_actual

        self._unsubs.append(async_track_time_interval(self.hass, self._async_sample, timedelta(seconds=SAMPLE_INTERVAL_SEC)))
        self._unsubs.append(async_track_time_interval(self.hass, self._async_control, timedelta(minutes=CONTROL_INTERVAL_MIN)))
        if self.cfg.get(CONF_AC_PHASE_POWER):
            self._unsubs.append(
                async_track_state_change_event(self.hass, [self.cfg[CONF_AC_PHASE_POWER]], self._on_phase_power)
            )
        self.hass.async_create_task(self._async_control(dt_util.now()))

    async def async_stop(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()
        await self._async_save()

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "enabled": self.enabled,
                "precool_enabled": self.precool_enabled,
                "bands": self.bands,
                "window": {k: v.strftime("%H:%M") for k, v in self.window.items()},
                "models": {k: m.to_dict() for k, m in self.models.items()},
                "ac_power": self.ac_power.to_dict(),
                "totals": self.totals.to_dict(),
                "night": self.night.to_dict(),
                "nights": self.nights[-30:],
                "suggestions": self.suggestions,
                "was_in_window": self._was_in_window,
                "tog": self.tog,
                "notified_date": self._notified_date,
            }
        )

    # ------------------------------------------------------------------ listeners
    @callback
    def async_add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    @callback
    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    # ------------------------------------------------------------------ settings from entities
    async def async_set_enabled(self, on: bool) -> None:
        self.enabled = on
        autos = self.cfg.get(CONF_AUTOMATIONS_TO_DISABLE) or []
        if autos:
            await self.hass.services.async_call(
                "automation", "turn_off" if on else "turn_on", {"entity_id": autos}, blocking=True
            )
        if on:
            self.override_until = None
            self.expected_mode = self.ac_mode_actual
        await self._async_save()
        await self._async_control(dt_util.now())

    async def async_set_precool(self, on: bool) -> None:
        self.precool_enabled = on
        await self._async_save()
        self._notify()

    async def async_set_band(self, key: str, value: float) -> None:
        self.bands[key] = value
        if key.startswith("tier1_") and self.tog != TOG_MANUAL:
            if (self.bands["tier1_min"], self.bands["tier1_max"]) != self.tog_band(self.tog):
                self.tog = TOG_MANUAL  # hand-edited band wins
        await self._async_save()
        await self._async_control(dt_util.now())

    def tog_band(self, tog: str) -> tuple[float, float]:
        return band_for(tog, chart_from_options(self.cfg), float(self.cfg.get(CONF_TOG_MARGIN, 0.5)))

    async def async_set_tog(self, tog: str) -> None:
        self.tog = tog
        if tog != TOG_MANUAL:
            self.bands["tier1_min"], self.bands["tier1_max"] = self.tog_band(tog)
        self.night.tog = tog if self.in_window(dt_util.now()) else self.night.tog
        await self._async_save()
        await self._async_control(dt_util.now())

    def tonight(self, now: datetime) -> tuple[datetime, datetime]:
        """Start/end of the current overnight window, or the next one if outside it."""
        s, e = self.window["window_start"], self.window["window_end"]
        start = self.window_start_for(now)
        if not self.in_window(now) and start < now:
            start += timedelta(days=1)
        end = start.replace(hour=e.hour, minute=e.minute)
        if end <= start:
            end += timedelta(days=1)
        return start, end

    def _update_tog_recommendation(self, now: datetime, inp: PlanInput) -> None:
        if self._tog_rec_at and now - self._tog_rec_at < timedelta(minutes=30) and self.tog_rec:
            return
        t1 = next((z for z in self.zones if z.tier == 1), None)
        temp = self.zone_temp(t1) if t1 else None
        if t1 is None or temp is None or not self.forecast:
            return
        start, end = self.tonight(now)
        hours = (end - now).total_seconds() / 3600
        if hours <= 0 or hours > 40:
            return
        settings = Settings(**{**vars(inp.settings), "horizon_h": hours})
        sim = PlanInput(**{**vars(inp), "settings": settings})
        traj = project_passive(sim, ZoneInput(t1.climate_id, t1.name, 1, temp, self.models[t1.climate_id]))
        overnight = [v for w, v in traj if start <= w <= end]
        rec = recommend(overnight, chart_from_options(self.cfg))
        if rec:
            rec["night_of"] = start.strftime("%Y-%m-%d")
            rec["current_tog"] = self.tog
            if self.tog != TOG_MANUAL and self.tog != rec["tog"]:
                lo, hi = self.tog_band(self.tog)
                rec["note"] = (
                    f"Room would naturally sit {rec['natural_min']}–{rec['natural_max']}°C. "
                    f"Keeping {self.tog} TOG ({lo}–{hi}°C) means the AC will be doing more of the work."
                )
            self.tog_rec = rec
            self._tog_rec_at = now

    async def _maybe_notify(self, now: datetime) -> None:
        svc = self.cfg.get(CONF_NOTIFY_SERVICE)
        if not svc or not self.tog_rec:
            return
        h, m = str(self.cfg.get(CONF_NOTIFY_TIME, "17:00")).split(":")[:2]
        today = now.strftime("%Y-%m-%d")
        if self._notified_date == today or (now.hour, now.minute) < (int(h), int(m)):
            return
        start, _ = self.tonight(now)
        if now > start + timedelta(hours=3):
            self._notified_date = today  # too late to be useful (e.g. after a restart)
            return
        self._notified_date = today
        r = self.tog_rec
        t1 = next((z for z in self.zones if z.tier == 1), None)
        name = t1.name if t1 else "Tier 1 room"
        lo, hi = self.tog_band(r["tog"])
        msg = (
            f"{name}: {r['tog']} TOG tonight. Room would drift {r['natural_max']}→{r['natural_min']}°C on its own; "
            f"the AC will hold {lo}–{hi}°C."
        )
        if self.tog != TOG_MANUAL and self.tog != r["tog"]:
            msg += f" (Currently set to {self.tog} TOG — change it on the dashboard if you switch suits.)"
        domain, service = svc.split(".", 1) if "." in svc else ("notify", svc)
        await self._call(domain, service, {"title": "Sleepwear tonight", "message": msg})
        await self._async_save()

    async def async_set_window(self, key: str, value: time) -> None:
        self.window[key] = value
        await self._async_save()
        await self._async_control(dt_util.now())

    # ------------------------------------------------------------------ time helpers
    def in_window(self, when: datetime) -> bool:
        s, e = self.window["window_start"], self.window["window_end"]
        t = when.timetz().replace(tzinfo=None)
        return (t >= s or t < e) if s > e else (s <= t < e)

    def window_start_for(self, now: datetime) -> datetime:
        s = self.window["window_start"]
        today = now.replace(hour=s.hour, minute=s.minute, second=0, microsecond=0)
        if self.in_window(now) and now < today:
            return today - timedelta(days=1)
        return today if (now <= today or self.in_window(now)) else today + timedelta(days=1)

    def band(self, tier: int, when: datetime) -> tuple[float, float]:
        if self.in_window(when):
            return self.bands[f"tier{tier}_min"], self.bands[f"tier{tier}_max"]
        return self.bands["day_min"], self.bands["day_max"]

    # ------------------------------------------------------------------ readings
    def zone_temp(self, z: Zone) -> float | None:
        if z.sensor_id:
            v = _f(self.hass.states.get(z.sensor_id))
            if v is not None:
                return v
        st = self.hass.states.get(z.climate_id)
        if st is None:
            return None
        v = st.attributes.get("current_temperature")
        return float(v) if v is not None else None

    def _read_weather(self) -> None:
        st = self.hass.states.get(self.cfg[CONF_WEATHER])
        if st is None:
            return
        t = st.attributes.get("temperature")
        self.outdoor = float(t) if t is not None else None
        c = st.attributes.get("cloud_coverage")
        self.cloud = float(c) if c is not None else None

    async def _async_refresh_forecast(self, now: datetime) -> None:
        if self._forecast_at and now - self._forecast_at < timedelta(minutes=30) and self.forecast:
            return
        try:
            resp = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": self.cfg[CONF_WEATHER], "type": "hourly"},
                blocking=True,
                return_response=True,
            )
            rows = (resp or {}).get(self.cfg[CONF_WEATHER], {}).get("forecast", [])
            fc = []
            for r in rows:
                when = dt_util.parse_datetime(r["datetime"])
                if when is None or r.get("temperature") is None:
                    continue
                fc.append(ForecastPoint(dt_util.as_local(when), float(r["temperature"]), r.get("cloud_coverage")))
            if fc:
                self.forecast = fc
                self._forecast_at = now
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Forecast refresh failed: %s", err)

    # ------------------------------------------------------------------ phase power -> AC power learning
    @callback
    def _on_phase_power(self, event: Event) -> None:
        v = _f(event.data.get("new_state"))
        if v is None:
            return
        export_pos = v if self.cfg.get(CONF_GRID_IMPORT_NEGATIVE, True) else -v
        self.ac_power.add_sample(dt_util.now(), export_pos)

    # ------------------------------------------------------------------ 1-minute sampling
    def _track_main_mode(self, now: datetime) -> None:
        main = self.hass.states.get(self.cfg[CONF_MAIN_CLIMATE])
        mode = main.state if main and main.state in ACTIVE_MODES else "off"
        if mode != self.ac_mode_actual:
            if mode == "off":
                self.last_stop = now
                self.running_precool = False
                self.ac_power.mark("stop", now)
            else:
                self.ac_since = now
                if self.ac_mode_actual == "off":
                    self.ac_power.mark("start", now)
                    self.night.starts += 1 if self.in_window(now) else 0
                    self.totals.starts += 1
            self.ac_mode_actual = mode

    async def _async_sample(self, now: datetime) -> None:
        now = dt_util.as_local(now)
        self._track_main_mode(now)

        grid = _f(self.hass.states.get(self.cfg.get(CONF_GRID_POWER))) if self.cfg.get(CONF_GRID_POWER) else None
        if grid is not None and not self.cfg.get(CONF_GRID_IMPORT_NEGATIVE, True):
            grid = -grid
        self.grid_w_export = grid
        ac_on = self.ac_mode_actual in ACTIVE_MODES
        self.ac_watts_now = self.ac_power.ac_watts if ac_on else 0.0
        solar_w, grid_w = split_solar_grid(self.ac_watts_now, grid)
        self.solar_w_now = solar_w
        hours = SAMPLE_INTERVAL_SEC / 3600
        self.totals.add(self.ac_watts_now, solar_w, grid_w, hours)

        in_win = self.in_window(now)
        if in_win:
            if not self._was_in_window:
                self.night = NightRecorder(started=now.isoformat(), tog=self.tog)
            ws = self.window_start_for(now)
            minute_of_window = int((now - ws).total_seconds() // 60)
            samples = {}
            for z in self.zones:
                t = self.zone_temp(z)
                if t is None:
                    continue
                lo, hi = self.band(z.tier, now)
                samples[z.name] = (z.tier, ZoneSample(t, lo, hi))
            self.night.sample(samples, ac_on, minute_of_window)
            self.night.ac_kwh += self.ac_watts_now * hours / 1000
            self.night.solar_kwh += solar_w * hours / 1000
            self.night.grid_kwh += grid_w * hours / 1000
            # forecast accuracy for tier-1 (predicted 60 min earlier)
            t1 = next((z for z in self.zones if z.tier == 1), None)
            due = [p for p in self._pending_preds if abs((p[0] - now).total_seconds()) <= 150]
            if t1 and due:
                actual = self.zone_temp(t1)
                if actual is not None:
                    self.night.pred_errors.append(abs(due[0][1] - actual))
                self._pending_preds = [p for p in self._pending_preds if p not in due]
        elif self._was_in_window:
            await self._finish_night(now)
        self._was_in_window = in_win
        self._pending_preds = [p for p in self._pending_preds if p[0] > now - timedelta(minutes=5)]

        await self._maybe_notify(now)
        self._save_counter += 1
        if self._save_counter >= 10:
            self._save_counter = 0
            await self._async_save()
        self._notify()

    async def _finish_night(self, now: datetime) -> None:
        if self.night.minutes < 60:
            return
        label = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        summary = summarise(
            self.night, label, float(self.cfg.get(CONF_IMPORT_TARIFF) or 0), float(self.cfg.get(CONF_FEED_IN_TARIFF) or 0)
        )
        self.nights.append(summary)
        self.nights = self.nights[-30:]
        self.suggestions = suggestions(self.nights, self.cfg, self.precool_enabled)
        self.night = NightRecorder()
        await self._async_save()

    # ------------------------------------------------------------------ learning (every control cycle)
    def _learn(self, now: datetime) -> None:
        since_stop = (now - self.last_stop) if self.last_stop else timedelta(days=1)
        for z in self.zones:
            temp = self.zone_temp(z)
            if temp is None:
                continue
            if z.prev is not None:
                t0, temp0 = z.prev
                dt_h = (now - t0).total_seconds() / 3600
                if 3 / 60 <= dt_h <= 15 / 60 and self.outdoor is not None:
                    observed = (temp - temp0) / dt_h
                    mid = t0 + (now - t0) / 2
                    sol = solar_factor(mid, self.hass.config.latitude, self.hass.config.longitude, self.cloud)
                    m = self.models[z.climate_id]
                    passive = m.passive_rate(temp0, self.outdoor, sol)
                    if self._last_learn_mode == "off" and self.ac_mode_actual == "off" and since_stop > timedelta(minutes=20):
                        m.update_passive(temp0, self.outdoor, sol, observed)
                    elif self._last_learn_mode == self.ac_mode_actual and self.ac_mode_actual in ACTIVE_MODES:
                        zst = self.hass.states.get(z.climate_id)
                        tgt = zst.attributes.get("temperature") if zst else None
                        zone_open = zst is not None and zst.state != "off"
                        needs = tgt is not None and (temp0 > tgt + 0.3 if self.ac_mode_actual == "cool" else temp0 < tgt - 0.3)
                        if zone_open and needs and self.ac_since and now - self.ac_since > timedelta(minutes=10):
                            m.update_active(self.ac_mode_actual, observed, passive)
            z.prev = (now, temp)
        self._last_learn_mode = self.ac_mode_actual

    # ------------------------------------------------------------------ control cycle
    def _settings(self) -> Settings:
        c = self.cfg
        return Settings(
            horizon_h=float(c[CONF_HORIZON]),
            cool_depth=float(c[CONF_COOL_DEPTH]),
            heat_depth=float(c[CONF_HEAT_DEPTH]),
            precool_depth=float(c[CONF_PRECOOL_DEPTH]),
            min_run_min=int(c[CONF_MIN_RUN]),
            min_off_min=int(c[CONF_MIN_OFF]),
            start_lag_min=int(c[CONF_START_LAG]),
            tolerance_c=float(c[CONF_TOLERANCE]),
            min_open_zones=int(c[CONF_MIN_OPEN_ZONES]),
            precool_enabled=self.precool_enabled,
            precool_start_hour=int(c[CONF_PRECOOL_START_HOUR]),
            precool_min_outdoor=float(c[CONF_PRECOOL_MIN_OUTDOOR]),
            precool_min_surplus_w=float(c[CONF_PRECOOL_MIN_SURPLUS]),
            allow_cooling=bool(c[CONF_ALLOW_COOLING]),
            allow_heating=bool(c[CONF_ALLOW_HEATING]),
        )

    async def _async_control(self, now: datetime) -> None:
        now = dt_util.as_local(now)
        try:
            self._track_main_mode(now)
            self._read_weather()
            self._learn(now)
            await self._async_refresh_forecast(now)

            surplus = None
            if self.grid_w_export is not None:
                surplus = self.grid_w_export + (self.ac_watts_now if self.ac_mode_actual in ACTIVE_MODES else 0)

            inp = PlanInput(
                now=now,
                zones=[ZoneInput(z.climate_id, z.name, z.tier, self.zone_temp(z), self.models[z.climate_id]) for z in self.zones],
                forecast=self.forecast,
                outdoor_now=self.outdoor,
                cloud_now=self.cloud,
                lat=self.hass.config.latitude,
                lon=self.hass.config.longitude,
                band_fn=self.band,
                window_start=self.window_start_for(now),
                ac_mode=self.ac_mode_actual,
                ac_since=self.ac_since,
                last_stop=self.last_stop,
                running_precool=self.running_precool,
                solar_surplus_w=surplus,
                settings=self._settings(),
            )
            decision = plan(inp)
            self.decision = decision
            self._update_tog_recommendation(now, inp)
            t1 = next((z for z in self.zones if z.tier == 1), None)
            if t1:
                traj = decision.predictions.get(t1.climate_id) or []
                p60 = next((v for w, v in traj if w >= now + timedelta(minutes=60)), None)
                if p60 is not None:
                    self._pending_preds.append((now + timedelta(minutes=60), p60))

            # manual override detection
            if self.enabled and self.expected_mode is not None and self.ac_mode_actual != self.expected_mode:
                if self.last_cmd is None or now - self.last_cmd > timedelta(minutes=3):
                    self.override_until = now + timedelta(minutes=int(self.cfg[CONF_OVERRIDE_MINUTES]))
                    self.expected_mode = None
                    _LOGGER.info("Manual AC change detected; pausing until %s", self.override_until)

            if not self.enabled:
                self.status = "disabled"
                self.reason = f"Shadow mode — would {decision.action}: {decision.reason}"
            elif self.override_until and now < self.override_until:
                self.status = "manual_override"
                self.reason = f"Manual change detected — paused until {self.override_until.strftime('%H:%M')}. Planner says: {decision.reason}"
            else:
                if self.override_until:
                    self.override_until = None
                    self.expected_mode = self.ac_mode_actual
                await self._actuate(decision, now)
                self.status = ("precooling" if decision.precool else f"{decision.action}ing") if decision.action != "off" else "idle"
                self.reason = decision.reason
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Drift control cycle failed")
            self.status = "error"
        self._notify()

    async def _call(self, domain: str, service: str, data: dict) -> None:
        try:
            await self.hass.services.async_call(domain, service, data, blocking=True)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Service %s.%s %s failed: %s", domain, service, data, err)

    async def _apply_zones(self, d: Decision) -> None:
        for z in self.zones:
            cmd = d.zone_commands.get(z.climate_id)
            st = self.hass.states.get(z.climate_id)
            if cmd is None or st is None:
                continue
            if cmd.on:
                if st.state == "off":
                    on_mode = next((m for m in st.attributes.get("hvac_modes", []) if m != "off"), "heat_cool")
                    await self._call("climate", "set_hvac_mode", {"entity_id": z.climate_id, "hvac_mode": on_mode})
                if st.attributes.get("temperature") != cmd.setpoint:
                    await self._call("climate", "set_temperature", {"entity_id": z.climate_id, "temperature": cmd.setpoint})
            elif st.state != "off":
                await self._call("climate", "set_hvac_mode", {"entity_id": z.climate_id, "hvac_mode": "off"})
        sel = self.cfg.get(CONF_MYZONE_SELECT)
        if sel and d.myzone:
            sst = self.hass.states.get(sel)
            zone = next((z for z in self.zones if z.climate_id == d.myzone), None)
            if sst and zone:
                option = next(
                    (o for o in sst.attributes.get("options", []) if o.lower() == zone.climate_id.split("_")[-1].lower() or o.lower() == zone.name.lower()),
                    None,
                )
                if option and sst.state != option:
                    await self._call("select", "select_option", {"entity_id": sel, "option": option})

    async def _actuate(self, d: Decision, now: datetime) -> None:
        main = self.cfg[CONF_MAIN_CLIMATE]
        if d.action == "off":
            if self.ac_mode_actual != "off":
                await self._call("climate", "set_hvac_mode", {"entity_id": main, "hvac_mode": "off"})
                self.last_cmd = now
            self.expected_mode = "off"
            return
        await self._apply_zones(d)
        drv = d.zone_commands.get(d.driver_zone) if d.driver_zone else None
        mst = self.hass.states.get(main)
        if self.ac_mode_actual != d.action:
            await self._call("climate", "set_hvac_mode", {"entity_id": main, "hvac_mode": d.action})
            self.last_cmd = now
            self.running_precool = d.precool
        if drv and mst and mst.attributes.get("temperature") != drv.setpoint:
            await self._call("climate", "set_temperature", {"entity_id": main, "temperature": drv.setpoint})
        self.expected_mode = d.action

    # ------------------------------------------------------------------ for entities
    def tier1_prediction(self, minutes: int) -> float | None:
        if not self.decision:
            return None
        t1 = next((z for z in self.zones if z.tier == 1), None)
        if not t1:
            return None
        traj = self.decision.predictions.get(t1.climate_id) or []
        if not traj:
            return None
        target = traj[0][0] + timedelta(minutes=minutes)
        return next((round(v, 1) for w, v in traj if w >= target), round(traj[-1][1], 1))

    def outdoor_forecast_summary(self) -> list[dict]:
        if not self.decision:
            return []
        now = dt_util.now()
        out = []
        for h in range(0, 13):
            w = now + timedelta(hours=h)
            inp = PlanInput(now, [], self.forecast, self.outdoor, self.cloud, 0, 0, self.band, now, "off", None, None)
            t, _ = outdoor_at(inp, w)
            out.append({"time": w.strftime("%H:%M"), "outdoor": round(t, 1)})
        return out
