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
    CONF_TIER2_CAP,
    CONF_TIER2_COOLDOWN,
    CONF_MYZONE_SELECT,
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
    CONF_OUTDOOR_SENSOR,
    CONF_TIER1_DOOR_SENSOR,
    CONF_TIER1_SENSOR,
    CONF_TIER1_ZONE,
    FORECAST_BIAS_FADE_H,
    OUTDOOR_SENSOR_MAX_AGE_MIN,
    CONF_TIER2_ZONES,
    CONF_TIER3_ZONES,
    CONF_CONSTANT_ZONES,
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
from .model import DoorModels, ZoneModel, solar_factor
from .planner import Decision, ForecastPoint, PlanInput, Settings, ZoneInput, outdoor_at, plan, project_passive
from .rules import Rule, SensorPresence, WeeklyBlocks, presence_active, resolve_band, rule_active
from .review import NightRecorder, ZoneSample, suggestions, summarise
from . import watchdog
from .tog import TOG_MANUAL, band_for, chart_from_options, recommend

_LOGGER = logging.getLogger(__name__)
ACTIVE_MODES = ("cool", "heat")
# The door must have sat in one state this long before a sample counts towards its own model:
# air exchange changes at once, but the walls and furniture take a while to follow.
DOOR_SETTLE_MIN = 10
# How long the door must have been open, and the run going, before asking for it to be shut.
DOOR_NUDGE_AFTER_MIN = 3
# The tier 1 sensor must be unavailable this long (overnight) before we say so; it reports on change only,
# with gaps of over an hour when the room is steady, so "hasn't reported" can't be used as the test.
SENSOR_DOWN_ALERT_MIN = 20


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
    def __init__(
        self,
        climate_id: str,
        tier: int,
        sensor_id: str | None,
        name: str,
        constant: bool = False,
        door_sensor_id: str | None = None,
    ) -> None:
        self.climate_id = climate_id
        self.door_sensor_id = door_sensor_id
        self.tier = tier
        self.sensor_id = sensor_id
        self.name = name
        self.constant = constant
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
        self.door_models: dict[str, DoorModels] = {z.climate_id: DoorModels() for z in self.zones if z.door_sensor_id}
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
        self.outdoor_source: str | None = None
        self.outdoor_model: float | None = None
        self.cloud: float | None = None
        self.ac_mode_actual = "off"
        self.ac_since: datetime | None = None
        self.last_stop: datetime | None = None
        self.running_precool = False
        self.expected_mode: str | None = None
        self.last_cmd: datetime | None = None
        # Only a run Drift started is held to the minimum run time (see PlanInput.run_is_ours).
        self.run_is_ours: bool = False
        # When Drift's last command was seen to land. After that, any departure from it is a
        # person, however soon it comes (the 3-minute echo allowance only covers a command in flight).
        self._landed_at: datetime | None = None
        # True from a restart until the AC entity first reports a state: not loaded yet isn't an outage.
        self._awaiting_first_state = False
        self._loaded: dict = {}
        self.override_until: datetime | None = None
        # MyAir can drop commands or go unavailable for a while. These track that so a
        # glitch is retried and reported rather than mistaken for someone changing the AC.
        self.ac_unavailable_since: datetime | None = None
        self.ac_back_at: datetime | None = None
        self.pending: dict | None = None  # {"mode", "first", "last", "attempts"}
        self.fault: str | None = None
        self._alerts: set[str] = set()
        # sleep-safe watchdog: recent tier 1 readings with the band each was taken under (not persisted)
        self._t1_series: list[watchdog.Reading] = []
        self._solar_low_since: datetime | None = None  # when the spare solar fell below the pre-cool stop level
        self._zone_open_since: dict[str, datetime] = {}  # tier 2 run-cap clocks (IDEAS 16)
        self._zone_capped_until: dict[str, datetime] = {}
        self._t1_sensor_down_since: datetime | None = None
        self._last_control: datetime | None = None
        self.bootstrap_info: dict | None = None
        # daytime comfort rules (config subentries)
        self.rules: list[Rule] = [
            Rule.from_subentry(sid, sub.title, dict(sub.data))
            for sid, sub in entry.subentries.items()
            if sub.subentry_type == "comfort_rule"
        ]
        self._schedules: dict[str, WeeklyBlocks] = {}
        self._presence: dict[tuple[str, str], dict] = {}  # (rule_id, sensor) -> {on_since, last_ok}
        self.presence_now: dict[str, bool] = {}
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

        constants = set(self.cfg.get(CONF_CONSTANT_ZONES) or [])
        t1 = self.cfg.get(CONF_TIER1_ZONE)
        if t1:
            self.zones.append(
                Zone(t1, 1, self.cfg.get(CONF_TIER1_SENSOR), nm(t1), t1 in constants, self.cfg.get(CONF_TIER1_DOOR_SENSOR))
            )
        for eid in self.cfg.get(CONF_TIER2_ZONES, []) or []:
            if eid != t1:
                self.zones.append(Zone(eid, 2, None, nm(eid), eid in constants))
        for eid in self.cfg.get(CONF_TIER3_ZONES, []) or []:
            if eid != t1 and eid not in (self.cfg.get(CONF_TIER2_ZONES) or []):
                self.zones.append(Zone(eid, 3, None, nm(eid), eid in constants))

    async def async_start(self) -> None:
        data = await self._store.async_load() or {}
        self._loaded = data
        self.enabled = data.get("enabled", False)
        self.precool_enabled = data.get("precool_enabled", True)
        self.bands.update(data.get("bands", {}))
        for k, v in (data.get("window") or {}).items():
            self.window[k] = _parse_time(v)
        for z in self.zones:
            self.models[z.climate_id] = ZoneModel.from_dict((data.get("models") or {}).get(z.climate_id))
            if z.door_sensor_id:
                self.door_models[z.climate_id] = DoorModels.from_dict((data.get("door_models") or {}).get(z.climate_id))
        self.ac_power = AcPowerLearner.from_dict(data.get("ac_power"))
        self.totals = EnergyTotals.from_dict(data.get("totals"))
        self.night = NightRecorder.from_dict(data.get("night"))
        self.nights = data.get("nights", [])
        self.suggestions = data.get("suggestions", [])
        self._was_in_window = data.get("was_in_window", False)
        self.tog = data.get("tog", TOG_MANUAL)
        self._notified_date = data.get("notified_date")
        self.bootstrap_info = data.get("bootstrap")
        if self.tog != TOG_MANUAL:
            self.bands["tier1_min"], self.bands["tier1_max"] = self.tog_band(self.tog)  # chart may have been edited

        main = self.hass.states.get(self.cfg[CONF_MAIN_CLIMATE])
        self.ac_mode_actual = main.state if main and main.state in ACTIVE_MODES else "off"
        if self.ac_mode_actual != "off":
            # last_changed is UTC; the reasons shown to people use local clock time
            self.ac_since = dt_util.as_local(main.last_changed)
        when = lambda k: dt_util.as_local(dt_util.parse_datetime(data[k])) if data.get(k) else None  # noqa: E731
        self.override_until = when("override_until")
        self.last_stop = when("last_stop")
        if data.get("ac_mode") == self.ac_mode_actual and when("ac_since"):
            # still in the run we saved: keep its true start for the minimum-run guard
            self.ac_since = when("ac_since")
        # A run is only Drift's if we saved it as such and it's still the same run. A missing key
        # (saved by an older version) means an inherited run, which is never held.
        self.run_is_ours = bool(data.get("run_is_ours", False)) and data.get("ac_mode") == self.ac_mode_actual
        self.expected_mode = self.ac_mode_actual
        if main is None or main.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            # A restart isn't an outage: the entity just isn't loaded yet. Wait for its first
            # state instead of starting the "recently back" grace that would resend a person's change.
            self._awaiting_first_state = True

        self._unsubs.append(async_track_time_interval(self.hass, self._async_sample, timedelta(seconds=SAMPLE_INTERVAL_SEC)))
        self._unsubs.append(async_track_time_interval(self.hass, self._async_control, timedelta(minutes=CONTROL_INTERVAL_MIN)))
        if self.cfg.get(CONF_AC_PHASE_POWER):
            self._unsubs.append(
                async_track_state_change_event(self.hass, [self.cfg[CONF_AC_PHASE_POWER]], self._on_phase_power)
            )
        if self.rules:
            await self._async_refresh_schedules()
            self._unsubs.append(async_track_time_interval(self.hass, self._async_refresh_schedules, timedelta(hours=1)))
            sched_ids = sorted({r.schedule for r in self.rules if r.trigger == "schedule" and r.schedule})
            if sched_ids:
                self._unsubs.append(async_track_state_change_event(self.hass, sched_ids, self._on_schedule_change))
            pres_ids = sorted({e for r in self.rules if r.trigger == "presence" for e in r.presence})
            if pres_ids:
                self._unsubs.append(async_track_state_change_event(self.hass, pres_ids, self._on_presence_change))
            self._update_presence(dt_util.now())
        t1 = next((z for z in self.zones if z.tier == 1), None)
        if t1:
            watch = [t1.sensor_id] if t1.sensor_id else []
            self._unsubs.append(
                async_track_state_change_event(self.hass, watch + [t1.climate_id], self._on_tier1_change)
            )
        if not self.bootstrap_info:
            self.hass.async_create_task(self.async_relearn())
        else:
            self.hass.async_create_task(self._async_control(dt_util.now()))

    @callback
    def _on_schedule_change(self, event: Event) -> None:
        # A block starting or ending, or someone editing the helper: re-read and re-plan.
        async def _go():
            await self._async_refresh_schedules()
            await self._async_control(dt_util.now())

        self.hass.async_create_task(_go())

    @callback
    def _on_presence_change(self, event: Event) -> None:
        self._update_presence(dt_util.now())

    @callback
    def _on_tier1_change(self, event: Event) -> None:
        """Check straight away if the tier 1 room has left its band, rather than waiting for
        the next 5-minute cycle. Rate-limited so a chatty sensor can't hammer the AC."""
        t1 = next((z for z in self.zones if z.tier == 1), None)
        temp = self.zone_temp(t1) if t1 else None
        if temp is None:
            return
        now = dt_util.now()
        lo, hi = self.band(t1, now)
        if lo <= temp <= hi:
            return
        if self._last_control and now - self._last_control < timedelta(minutes=2):
            return
        self.hass.async_create_task(self._async_control(now))

    async def async_relearn(self) -> None:
        """Rebuild every zone's model from the last week of recorder history."""
        from functools import partial

        from homeassistant.components.recorder import get_instance, history

        from .bootstrap import fit_from_history, merge_outdoor_history

        end = dt_util.now()
        start = end - timedelta(days=7)
        main_id = self.cfg[CONF_MAIN_CLIMATE]
        ids = {main_id, self.cfg[CONF_WEATHER]}
        if self.cfg.get(CONF_OUTDOOR_SENSOR):
            ids.add(self.cfg[CONF_OUTDOOR_SENSOR])
        for z in self.zones:
            ids.add(z.climate_id)
            if z.sensor_id:
                ids.add(z.sensor_id)
        try:
            hist = await get_instance(self.hass).async_add_executor_job(
                partial(
                    history.get_significant_states,
                    self.hass,
                    start,
                    end,
                    sorted(ids),
                    significant_changes_only=False,
                    minimal_response=False,
                    no_attributes=False,
                )
            )
        except Exception as err:  # noqa: BLE001  (no recorder, or it failed: keep learning online)
            _LOGGER.warning("Couldn't read history to train the room models: %s", err)
            self.bootstrap_info = {"at": end.isoformat(), "error": str(err)}
            await self._async_save()
            await self._async_control(dt_util.now())
            return

        def rows(eid):
            return [st for st in hist.get(eid, []) if hasattr(st, "state")]

        modes = [
            (st.last_updated, st.state if st.state in ACTIVE_MODES else "off")
            for st in rows(main_id)
            if st.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
        ]
        outdoor = []
        for st in rows(self.cfg[CONF_WEATHER]):
            t = st.attributes.get("temperature")
            if t is not None:
                outdoor.append((st.last_updated, float(t), st.attributes.get("cloud_coverage")))
        outdoor, observed_from = merge_outdoor_history(
            outdoor, [(st.last_updated, _f(st)) for st in rows(self.cfg.get(CONF_OUTDOOR_SENSOR))]
        )
        info = {"at": end.isoformat(), "days": 7, "zones": {}, "outdoor_observed_from": observed_from}
        for z in self.zones:
            if z.sensor_id:
                temps = [(st.last_updated, _f(st)) for st in rows(z.sensor_id)]
            else:
                temps = [(st.last_updated, st.attributes.get("current_temperature")) for st in rows(z.climate_id)]
            temps = [(t, float(v)) for t, v in temps if v is not None]
            zstate = [
                (st.last_updated, (st.state not in ("off", STATE_UNAVAILABLE, STATE_UNKNOWN), st.attributes.get("temperature")))
                for st in rows(z.climate_id)
            ]
            model = ZoneModel()
            counts = await self.hass.async_add_executor_job(
                fit_from_history,
                model,
                temps,
                modes,
                outdoor,
                zstate,
                self.hass.config.latitude,
                self.hass.config.longitude,
                start,
                end,
            )
            # keep the fresh fit only if history actually had enough to say
            if counts["passive"] >= 50:
                self.models[z.climate_id] = model
            info["zones"][z.name] = {**counts, "used": counts["passive"] >= 50}
        self.bootstrap_info = info
        self._tog_rec_at = None
        await self._async_save()
        _LOGGER.info("Room models trained from history: %s", info["zones"])
        await self._async_control(dt_util.now())

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
                "door_models": {k: m.to_dict() for k, m in self.door_models.items()},
                "ac_power": self.ac_power.to_dict(),
                "totals": self.totals.to_dict(),
                "night": self.night.to_dict(),
                "nights": self.nights[-30:],
                "suggestions": self.suggestions,
                "was_in_window": self._was_in_window,
                "tog": self.tog,
                "notified_date": self._notified_date,
                "bootstrap": self.bootstrap_info,
                # Kept across restarts and reloads: losing them let Drift undo a manual stop and
                # restart the compressor minutes after it last stopped.
                "override_until": self.override_until.isoformat() if self.override_until else None,
                "last_stop": self.last_stop.isoformat() if self.last_stop else None,
                "ac_since": self.ac_since.isoformat() if self.ac_since else None,
                "ac_mode": self.ac_mode_actual,
                "run_is_ours": self.run_is_ours,
                "expected_mode": self.expected_mode,
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
        return band_for(tog, chart_from_options(self.cfg))

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
        traj = project_passive(sim, self.zone_input(t1, temp))
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

    def tier_band(self, tier: int) -> tuple[float, float]:
        return self.bands[f"tier{tier}_min"], self.bands[f"tier{tier}_max"]

    def band(self, zone: Zone, when: datetime) -> tuple[float, float]:
        in_win = self.in_window(when)
        now = dt_util.now()
        rule_bands = [
            self.tier_band(1) if r.band_mode == "tier1" else (r.min, r.max)
            for r in self.rules
            if zone.climate_id in r.zones
            and rule_active(r, when, now, in_win, self._schedules, self.presence_now)
        ]
        return resolve_band(
            zone.tier, in_win, self.tier_band(zone.tier), (self.bands["day_min"], self.bands["day_max"]), rule_bands
        )

    def band_by_id(self, zone_id: str, when: datetime) -> tuple[float, float]:
        zone = next(z for z in self.zones if z.climate_id == zone_id)
        return self.band(zone, when)

    def active_rules(self, when: datetime) -> list[str]:
        in_win = self.in_window(when)
        return [r.name for r in self.rules if rule_active(r, when, when, in_win, self._schedules, self.presence_now)]

    # ------------------------------------------------------------------ rules: schedules and presence
    async def _async_refresh_schedules(self, *_args) -> None:
        ids = sorted({r.schedule for r in self.rules if r.trigger == "schedule" and r.schedule})
        for sid in ids:
            try:
                resp = await self.hass.services.async_call(
                    "schedule", "get_schedule", {"entity_id": sid}, blocking=True, return_response=True
                )
                self._schedules[sid] = WeeklyBlocks.from_service((resp or {}).get(sid, {}))
            except Exception as err:  # noqa: BLE001  (missing helper: rule falls back, never fails the cycle)
                if sid in self._schedules or sid not in getattr(self, "_schedule_warned", set()):
                    _LOGGER.warning("Couldn't read schedule %s for a comfort rule: %s", sid, err)
                    self._schedule_warned = getattr(self, "_schedule_warned", set()) | {sid}
                self._schedules.pop(sid, None)

    def _update_presence(self, now: datetime) -> None:
        for r in self.rules:
            if r.trigger != "presence":
                continue
            sensors = []
            for eid in r.presence:
                rec = self._presence.setdefault((r.rule_id, eid), {"on_since": None, "last_ok": None})
                st = self.hass.states.get(eid)
                is_on = st is not None and st.state == "on"
                if is_on:
                    rec["on_since"] = rec["on_since"] or st.last_changed
                    # Only a stay that met the dwell counts towards the hold afterwards.
                    if now - rec["on_since"] >= timedelta(minutes=r.dwell_minutes):
                        rec["last_ok"] = now
                else:
                    rec["on_since"] = None
                sensors.append(SensorPresence(is_on, rec["on_since"], rec["last_ok"]))
            self.presence_now[r.rule_id] = presence_active(r, now, sensors)

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
        self.outdoor_model = self.outdoor
        self.outdoor_source = self.cfg[CONF_WEATHER] if self.outdoor is not None else None
        # The weather entity's "current" temperature is a model value (met.no can sit 5°C off on a
        # rainy evening). A real observation wins while it is fresh; last_reported advances on every
        # poll even when the value is unchanged, so an unchanged reading doesn't look stale.
        sid = self.cfg.get(CONF_OUTDOOR_SENSOR)
        if sid:
            obs = self.hass.states.get(sid)
            v = _f(obs)
            if v is not None and dt_util.utcnow() - obs.last_reported <= timedelta(minutes=OUTDOOR_SENSOR_MAX_AGE_MIN):
                self.outdoor = v
                self.outdoor_source = sid
        c = st.attributes.get("cloud_coverage")
        self.cloud = float(c) if c is not None else None

    def planning_forecast(self, now: datetime) -> list[ForecastPoint]:
        """The forecast, nudged toward the real observation.

        If the observation says the model is running warm (or cool) right now, the next few hours
        probably are too. The gap is carried forward and faded out linearly over
        FORECAST_BIAS_FADE_H, after which the raw forecast is trusted again.
        """
        if self.outdoor_source == self.cfg[CONF_WEATHER] or self.outdoor is None or self.outdoor_model is None:
            return self.forecast
        bias = self.outdoor - self.outdoor_model
        out = []
        for p in self.forecast:
            h = (p.when - now).total_seconds() / 3600
            w = max(0.0, 1 - max(h, 0) / FORECAST_BIAS_FADE_H)
            out.append(ForecastPoint(p.when, p.temp + bias * w, p.cloud))
        return out

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
        if main is None or main.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            if self._awaiting_first_state:
                return  # still starting up: not an outage
            # Hold the last known mode: an outage isn't a stop, and isn't a manual change.
            if self.ac_unavailable_since is None:
                self.ac_unavailable_since = now
                _LOGGER.warning("Main AC unavailable; holding last known mode %s", self.ac_mode_actual)
            return
        mode = main.state if main.state in ACTIVE_MODES else "off"
        if self._awaiting_first_state:
            # First state since the restart. Not an outage return, so no "recently back" grace: a
            # person's change in the next few minutes must be respected, not resent. If the AC is
            # as we left it, carry on expecting what we expected; otherwise take it as the baseline.
            self._awaiting_first_state = False
            saved = self._loaded.get("expected_mode")
            self.expected_mode = saved if saved is not None and self._loaded.get("ac_mode") == mode else mode
            if mode != "off" and self._loaded.get("ac_mode") == mode:
                # The entity wasn't loaded when we started, so the placeholder "off" stood in for it.
                # It is the same run we saved: keep its true start and who began it, and don't count
                # it as a new start (or the compressor-cycle bookkeeping would).
                started = self._loaded.get("ac_since")
                parsed = dt_util.parse_datetime(started) if started else None
                self.ac_since = dt_util.as_local(parsed) if parsed else dt_util.as_local(main.last_changed)
                self.run_is_ours = bool(self._loaded.get("run_is_ours", False))
                self.ac_mode_actual = mode
        elif self.ac_unavailable_since is not None:
            self.ac_unavailable_since = None
            self.ac_back_at = now
        if mode != self.ac_mode_actual:
            if mode == "off" or mode != self.expected_mode:
                self.run_is_ours = False  # stopped, or started by someone else
            if mode == "off":
                self.last_stop = now
                self.hass.async_create_task(self._async_save())
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
        if self.rules:
            was = dict(self.presence_now)
            self._update_presence(now)
            if self.presence_now != was:
                self.hass.async_create_task(self._async_control(now))

        grid = _f(self.hass.states.get(self.cfg.get(CONF_GRID_POWER))) if self.cfg.get(CONF_GRID_POWER) else None
        if grid is not None and not self.cfg.get(CONF_GRID_IMPORT_NEGATIVE, True):
            grid = -grid
        self.grid_w_export = grid
        self._track_solar_dip(now)
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
                lo, hi = self.band(z, now)
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

        await self._check_ac_health(now)
        await self._maybe_notify(now)
        await self._maybe_door_nudge(now)
        await self._check_watchdog(now)
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

    # ------------------------------------------------------------------ door
    def door_closed(self, z: Zone) -> bool | None:
        """True shut, False open, None with no sensor or no reading. The sensor is an opening
        sensor, so "on" means open."""
        if not z.door_sensor_id:
            return None
        st = self.hass.states.get(z.door_sensor_id)
        if st is None or st.state not in ("on", "off"):
            return None
        return st.state == "off"

    def _door_settled(self, z: Zone, since: datetime) -> bool | None:
        """The door state if it hasn't changed since `since` minus a settling time, else None."""
        closed = self.door_closed(z)
        if closed is None:
            return None
        st = self.hass.states.get(z.door_sensor_id)
        if st is None or st.last_changed > since - timedelta(minutes=DOOR_SETTLE_MIN):
            return None
        return closed

    def _door_night(self, when: datetime) -> bool:
        s = Settings()
        return when.hour >= s.door_closed_from_hour or when.hour < s.door_closed_until_hour

    async def _maybe_door_nudge(self, now: datetime) -> None:
        """Tell the phones to shut the tier 1 room's door when Drift is running the AC for it.

        Once per run, by day only (from 18:00 a door left ajar counts as shut, and a night nudge
        would wake people), and only after the door and the run have both settled for a few
        minutes so walking through doesn't trigger it. Only for runs Drift started, and never
        while control is off."""
        t1 = next((z for z in self.zones if z.tier == 1 and z.door_sensor_id), None)
        if t1 is None:
            return
        if not (self.enabled and self.ac_mode_actual in ACTIVE_MODES and self.run_is_ours):
            await self._clear_alert("door_open")
            return
        st = self.hass.states.get(t1.door_sensor_id)
        zst = self.hass.states.get(t1.climate_id)
        if (
            self.door_closed(t1) is not False
            or st is None
            or zst is None
            or zst.state == "off"  # the room isn't part of this run
            or self._door_night(now)
            or self.ac_since is None
            or now - self.ac_since < timedelta(minutes=DOOR_NUDGE_AFTER_MIN)
            or now - st.last_changed < timedelta(minutes=DOOR_NUDGE_AFTER_MIN)
        ):
            return
        heating = self.ac_mode_actual == "heat"
        extra = " on the solar surplus" if self.running_precool else ""
        await self._alert(
            "door_open",
            f"Shut {t1.name}'s door",
            f"The AC is {'heating' if heating else 'cooling'} {t1.name}{extra} and its door is open. "
            f"Shutting it keeps the {'heat' if heating else 'cool'} in and the run shorter.",
        )

    def _surplus_now(self) -> float | None:
        """Export that would be available if the AC were off."""
        if self.grid_w_export is None:
            return None
        return self.grid_w_export + (self.ac_watts_now if self.ac_mode_actual in ACTIVE_MODES else 0)

    def _track_solar_dip(self, now: datetime) -> None:
        """Note how long the spare solar has been below the level at which a pre-cool should end.

        Sampled every minute (the control cycle is every 5), so a cloud that passes between cycles,
        or sits on one reading, doesn't end a pre-cool: the planner waits for the dip to last."""
        surplus = self._surplus_now()
        low = surplus is not None and surplus < 0.5 * float(self.cfg[CONF_PRECOOL_MIN_SURPLUS])
        if not low:
            self._solar_low_since = None
        elif self._solar_low_since is None:
            self._solar_low_since = now

    def zone_input(self, z: Zone, temp: float | None = None) -> ZoneInput:
        has_door = z.climate_id in self.door_models
        return ZoneInput(
            z.climate_id,
            z.name,
            z.tier,
            self.zone_temp(z) if temp is None else temp,
            self.models[z.climate_id],
            z.constant,
            self.door_models.get(z.climate_id) if has_door else None,
            self.door_closed(z) if has_door else None,
        )

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
                        # the door's own model learns only while the door has sat in one state since
                        # before this sample began, so a door swinging mid-sample teaches nothing
                        closed = self._door_settled(z, t0)
                        # ajar at night counts as shut: it is close enough, and the planner
                        # projects the night with the shut-door model
                        if closed is not None and self._door_night(mid):
                            closed = True
                        if closed is not None:
                            self.door_models[z.climate_id].for_state(closed).update_passive(temp0, self.outdoor, sol, observed)
                    elif self._last_learn_mode == self.ac_mode_actual and self.ac_mode_actual in ACTIVE_MODES:
                        zst = self.hass.states.get(z.climate_id)
                        tgt = zst.attributes.get("temperature") if zst else None
                        zone_open = zst is not None and zst.state != "off"
                        needs = tgt is not None and (temp0 > tgt + 0.3 if self.ac_mode_actual == "cool" else temp0 < tgt - 0.3)
                        if zone_open and needs and self.ac_since and now - self.ac_since > timedelta(minutes=10):
                            m.update_active(self.ac_mode_actual, observed, passive)
            z.prev = (now, temp)
        self._last_learn_mode = self.ac_mode_actual

    # ------------------------------------------------------------------ tier 2 run cap (IDEAS 16)
    def _update_caps(self, now: datetime) -> frozenset[str]:
        """Zone ids of tier 2 rooms that have been open for the cap and are in the cool-down.

        The clock runs while the AC is running and the zone is open, whoever opened it. At the cap
        the zone is shut and held shut for the cool-down, so a room whose sensor never reaches
        target can't keep the AC going. In memory only: a restart forgets it, which at worst
        gives that room one more capped run.
        """
        cap = int(self.cfg[CONF_TIER2_CAP])
        cool = timedelta(minutes=int(self.cfg[CONF_TIER2_COOLDOWN]))
        running = self.ac_mode_actual in ACTIVE_MODES
        capped: set[str] = set()
        for z in self.zones:
            zid = z.climate_id
            if z.tier != 2 or cap <= 0:
                self._zone_open_since.pop(zid, None)
                self._zone_capped_until.pop(zid, None)
                continue
            until = self._zone_capped_until.get(zid)
            if until is not None and now >= until:
                self._zone_capped_until.pop(zid, None)
                until = None
            if until is not None:
                capped.add(zid)
                continue
            st = self.hass.states.get(zid)
            if not running or st is None or st.state == "off":
                self._zone_open_since.pop(zid, None)
                continue
            since = self._zone_open_since.setdefault(zid, now)
            if now - since >= timedelta(minutes=cap):
                self._zone_capped_until[zid] = now + cool
                self._zone_open_since.pop(zid, None)
                capped.add(zid)
                _LOGGER.info("%s reached its %d-minute run cap; shut until %s", z.name, cap, (now + cool).strftime("%H:%M"))
        return frozenset(capped)

    def zone_capped_until(self, z: Zone) -> str | None:
        until = self._zone_capped_until.get(z.climate_id)
        return until.strftime("%H:%M") if until else None

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
            tier2_cap_min=int(c[CONF_TIER2_CAP]),
            tier2_cooldown_min=int(c[CONF_TIER2_COOLDOWN]),
            start_lag_min=int(c[CONF_START_LAG]),
            tolerance_c=float(c[CONF_TOLERANCE]),
            recovery_max_c=float(c[CONF_RECOVERY_MAX]),
            recovery_minutes=int(c[CONF_RECOVERY_MINUTES]),
            min_open_zones=int(c[CONF_MIN_OPEN_ZONES]),
            precool_enabled=self.precool_enabled,
            precool_start_hour=int(c[CONF_PRECOOL_START_HOUR]),
            precool_min_outdoor=float(c[CONF_PRECOOL_MIN_OUTDOOR]),
            precool_min_surplus_w=float(c[CONF_PRECOOL_MIN_SURPLUS]),
            allow_cooling=bool(c[CONF_ALLOW_COOLING]),
            allow_heating=bool(c[CONF_ALLOW_HEATING]),
        )

    def _setpoint_limits(self) -> dict[str, float]:
        """The main unit's own setpoint range, so a sent setpoint is one it accepts."""
        st = self.hass.states.get(self.cfg[CONF_MAIN_CLIMATE])
        out: dict[str, float] = {}
        if st:
            for key, field in (("min_temp", "min_setpoint"), ("max_temp", "max_setpoint")):
                try:
                    out[field] = float(st.attributes[key])
                except (KeyError, TypeError, ValueError):
                    pass
        return out

    async def _async_control(self, now: datetime) -> None:
        now = dt_util.as_local(now)
        try:
            self._track_main_mode(now)
            self._read_weather()
            self._learn(now)
            await self._async_refresh_forecast(now)

            surplus = self._surplus_now()
            capped = self._update_caps(now)

            inp = PlanInput(
                now=now,
                zones=[self.zone_input(z) for z in self.zones],
                forecast=self.planning_forecast(now),
                outdoor_now=self.outdoor,
                cloud_now=self.cloud,
                lat=self.hass.config.latitude,
                lon=self.hass.config.longitude,
                band_fn=self.band_by_id,
                window_start=self.window_start_for(now),
                ac_mode=self.ac_mode_actual,
                ac_since=self.ac_since,
                last_stop=self.last_stop,
                running_precool=self.running_precool,
                solar_surplus_w=surplus,
                capped_zones=capped,
                solar_low_min=(now - self._solar_low_since).total_seconds() / 60 if self._solar_low_since else 0.0,
                settings=self._settings(),
                run_is_ours=self.run_is_ours,
                **self._setpoint_limits(),
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

            # Manual override detection. A mismatch only counts as someone changing the AC
            # when the AC is reachable, Drift isn't still retrying a command, and it hasn't
            # just come back from an outage (MyAir can reappear in a different state).
            recently_back = self.ac_back_at is not None and now - self.ac_back_at < timedelta(minutes=10)
            if (
                self.enabled
                and self.expected_mode is not None
                and self.ac_mode_actual != self.expected_mode
                and self.ac_unavailable_since is None
                and self.pending is None
                and not recently_back
            ):
                # Within 3 minutes of a command a mismatch may just be a slow MyAir echo, but once
                # the command has landed, a later departure from it is a person.
                if self.last_cmd is None or now - self.last_cmd > timedelta(minutes=3) or self._landed_at is not None:
                    self.override_until = now + timedelta(minutes=int(self.cfg[CONF_OVERRIDE_MINUTES]))
                    self.expected_mode = None
                    _LOGGER.info("Manual AC change detected; pausing until %s", self.override_until)
                    await self._async_save()

            if not self.enabled:
                self.status = "disabled"
                self.reason = f"Shadow mode — would {decision.action}: {decision.reason}"
            elif self.override_until and now < self.override_until:
                self.status = "manual_override"
                self.reason = f"Manual change detected — paused until {self.override_until.strftime('%H:%M')}. Planner says: {decision.reason}"
            elif self._awaiting_first_state:
                # The AC hasn't reported since the restart: its mode is a placeholder, so sending
                # it anything now could start or stop a run we know nothing about.
                self.status = "waiting_for_ac"
                self.reason = f"Waiting for the AC to report after a restart. Planner says: {decision.reason}"
            else:
                if self.override_until:
                    self.override_until = None
                    self.expected_mode = self.ac_mode_actual
                await self._actuate(decision, now)
                self.status = ("precooling" if decision.precool else f"{decision.action}ing") if decision.action != "off" else "idle"
                self.reason = decision.reason
                if self.fault:
                    self.status = "ac_unreachable"
                    self.reason = f"{self.fault}. Planner says: {decision.reason}"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Drift control cycle failed")
            self.status = "error"
        self._last_control = now
        self._notify()

    async def _call(self, domain: str, service: str, data: dict) -> bool:
        try:
            await self.hass.services.async_call(domain, service, data, blocking=True)
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Service %s.%s %s failed: %s", domain, service, data, err)
            return False

    async def _send_mode(self, mode: str, now: datetime) -> None:
        """Send the main unit's mode and track it until the AC reports it."""
        main = self.cfg[CONF_MAIN_CLIMATE]
        await self._call("climate", "set_hvac_mode", {"entity_id": main, "hvac_mode": mode})
        self.last_cmd = now
        self._landed_at = None
        if self.pending and self.pending["mode"] == mode:
            self.pending["last"] = now
            self.pending["attempts"] += 1
        else:
            self.pending = {"mode": mode, "first": now, "last": now, "attempts": 1}

    async def _check_ac_health(self, now: datetime) -> None:
        """Every minute: confirm commands landed, retry quickly, and say so if the AC is unreachable."""
        if not self.enabled:
            self.pending = None
            self.fault = None
            return
        if self.pending:
            if self.ac_unavailable_since is None and self.ac_mode_actual == self.pending["mode"]:
                self.pending = None
                self._landed_at = now
            elif now - self.pending["last"] >= timedelta(seconds=90) and self.pending["attempts"] < 3:
                _LOGGER.warning("AC hasn't reported %s yet; resending (attempt %s)", self.pending["mode"], self.pending["attempts"] + 1)
                await self._send_mode(self.pending["mode"], now)
        problem = None
        if self.pending and self.pending["attempts"] >= 3 and now - self.pending["last"] >= timedelta(seconds=90):
            problem = f"The AC hasn't responded to '{self.pending['mode']}' since {self.pending['first'].strftime('%H:%M')} (3 attempts); retrying every 5 minutes"
        elif self.ac_unavailable_since and now - self.ac_unavailable_since >= timedelta(minutes=10):
            problem = f"The AC (MyAir) has been unavailable since {self.ac_unavailable_since.strftime('%H:%M')}"
        self.fault = problem
        if problem:
            t1 = next((z for z in self.zones if z.tier == 1), None)
            temp = self.zone_temp(t1) if t1 else None
            extra = ""
            if t1 and temp is not None:
                lo, hi = self.band(t1, now)
                extra = f" {t1.name} is {temp:.1f}°C (band {lo:g}–{hi:g})."
            await self._alert("ac_unreachable", "Drift can't control the AC", f"{problem}.{extra}")
        else:
            await self._clear_alert("ac_unreachable")

    # ------------------------------------------------------------------ sleep-safe watchdog
    def _t1_doing(self, now: datetime) -> str:
        """What Drift is doing about the tier 1 room, for the watchdog's message."""
        if self.fault:
            return "Drift can't reach the AC"
        if not self.enabled:
            return "Drift control is off"
        if self.override_until and now < self.override_until:
            return f"Drift is paused after a manual AC change until {self.override_until.strftime('%H:%M')}"
        if self.ac_mode_actual in ACTIVE_MODES and self.ac_since:
            verb = "cooling" if self.ac_mode_actual == "cool" else "heating"
            return f"the AC has been {verb} since {self.ac_since.strftime('%H:%M')}"
        return f"the AC is off (Drift: {self.reason or 'no reason yet'})"

    async def _check_watchdog(self, now: datetime) -> None:
        """Tell the phones if the tier 1 room has been out of its band for long enough overnight.

        Independent of the planner on purpose, and it runs whether control is on, off or paused:
        those are exactly the times nothing else is watching. Overnight window only."""
        t1 = next((z for z in self.zones if z.tier == 1), None)
        if t1 is None:
            return
        keys = ("tier1_out_of_band", "tier1_out_of_band_severe", "tier1_sensor_down")
        if not self.in_window(now):
            self._t1_series = []
            self._t1_sensor_down_since = None
            for k in keys:
                await self._clear_alert(k)
            return
        # a sensor that is unavailable falls back to the AC's own thermometer (zone_temp), but say so
        st = self.hass.states.get(t1.sensor_id) if t1.sensor_id else None
        if t1.sensor_id and (st is None or st.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)):
            self._t1_sensor_down_since = self._t1_sensor_down_since or now
            if now - self._t1_sensor_down_since >= timedelta(minutes=SENSOR_DOWN_ALERT_MIN):
                await self._alert(
                    "tier1_sensor_down",
                    f"{t1.name} sensor unavailable",
                    f"The {t1.name} temperature sensor has been unavailable since "
                    f"{self._t1_sensor_down_since.strftime('%H:%M')}. Drift is using the AC's own thermometer, which reads "
                    f"differently, until it returns.",
                )
        else:
            self._t1_sensor_down_since = None
            await self._clear_alert("tier1_sensor_down")

        temp = self.zone_temp(t1)
        if temp is not None:
            lo, hi = self.band(t1, now)
            self._t1_series = watchdog.trim([*self._t1_series, watchdog.Reading(now, temp, lo, hi)], now)
        breach = watchdog.assess(self._t1_series, now)
        if breach:
            lo, hi = self.band(t1, now)
            tog = f" (TOG {self.tog})" if self.tog != TOG_MANUAL else ""
            await self._alert(
                "tier1_out_of_band_severe" if breach.level == "severe" else "tier1_out_of_band",
                f"{t1.name} is {breach.side} its band",
                f"{t1.name} is {self.zone_temp(t1):.1f}°C, {breach.excess:.1f}°C {breach.side} its {lo:g}–{hi:g}°C band "
                f"and has been beyond it for {breach.minutes:.0f} minutes{tog}. {self._t1_doing(now).capitalize()}.",
            )
        elif watchdog.back_in_band(self._t1_series, now):
            await self._clear_alert("tier1_out_of_band")
            await self._clear_alert("tier1_out_of_band_severe")

    async def _alert(self, key: str, title: str, message: str) -> None:
        """Raise once per incident: a Home Assistant notification, plus the phone if set up."""
        if key in self._alerts:
            return
        self._alerts.add(key)
        await self._call(
            "persistent_notification", "create", {"notification_id": f"drift_{key}", "title": title, "message": message}
        )
        svc = self.cfg.get(CONF_NOTIFY_SERVICE)
        if svc:
            domain, service = svc.split(".", 1) if "." in svc else ("notify", svc)
            await self._call(domain, service, {"title": title, "message": message})

    async def _clear_alert(self, key: str) -> None:
        if key not in self._alerts:
            return
        self._alerts.discard(key)
        await self._call("persistent_notification", "dismiss", {"notification_id": f"drift_{key}"})

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
            elif st.state != "off" and not z.constant:  # belt and braces: never close a constant zone
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
            if self.ac_mode_actual != "off" or self.ac_unavailable_since is not None and self.expected_mode != "off":
                await self._send_mode("off", now)
            self.expected_mode = "off"
            return
        await self._apply_zones(d)
        drv = d.zone_commands.get(d.driver_zone) if d.driver_zone else None
        mst = self.hass.states.get(main)
        if self.ac_mode_actual != d.action or self.ac_unavailable_since is not None:
            await self._send_mode(d.action, now)
        # every cycle, not only at a start: a pre-cool that carries on as an ordinary run (a need
        # fell due while the solar faded) must stop being treated as one
        self.running_precool = d.precool
        if drv and mst and mst.attributes.get("temperature") != drv.setpoint:
            await self._call("climate", "set_temperature", {"entity_id": main, "temperature": drv.setpoint})
        self.expected_mode = d.action
        self.run_is_ours = True

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
            inp = PlanInput(now, [], self.planning_forecast(now), self.outdoor, self.cloud, 0, 0, self.band_by_id, now, "off", None, None, run_is_ours=self.run_is_ours)
            t, _ = outdoor_at(inp, w)
            out.append({"time": w.strftime("%H:%M"), "outdoor": round(t, 1)})
        return out
