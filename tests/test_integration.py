"""End-to-end test inside a real Home Assistant test instance."""
from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant, ServiceResponse, SupportsResponse
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

DOMAIN = "drift"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _zone(hass, name, temp, setpoint=24, state="heat_cool"):
    hass.states.async_set(
        f"climate.ac_{name}",
        state,
        {"friendly_name": f"AC {name.title()}", "current_temperature": temp, "temperature": setpoint, "hvac_modes": ["off", "heat_cool"]},
    )


async def _setup_states(hass, nursery=24.5, outdoor=25.0, forecast_temps=None):
    hass.config.latitude, hass.config.longitude = -33.87, 151.21
    await hass.config.async_set_time_zone("Australia/Sydney")
    hass.states.async_set("climate.ac", "off", {"friendly_name": "AC", "current_temperature": nursery, "temperature": 22, "hvac_modes": ["off", "cool", "heat", "fan_only", "dry"]})
    for n, t in [("nursery", nursery), ("master", 24.4), ("office", 24.4), ("lounge", 23.6), ("living", 23.7)]:
        _zone(hass, n, t)
    hass.states.async_set("select.ac_myzone", "Inactive", {"options": ["Inactive", "Office", "Lounge", "Living", "Nursery", "Master"]})
    hass.states.async_set("sensor.nursery_temperature", str(nursery), {"device_class": "temperature", "unit_of_measurement": "°C"})
    hass.states.async_set("weather.home", "cloudy", {"temperature": outdoor, "cloud_coverage": 90})
    hass.states.async_set("sensor.grid_power", "-500", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.grid_power_phase_b", "-200", {"unit_of_measurement": "W"})
    hass.states.async_set("automation.nursery_too_hot", "on", {})

    temps = forecast_temps or [22.3, 22.0, 21.5, 20.8, 20.2, 20.4, 19.9, 20.2]

    async def get_forecasts(call) -> ServiceResponse:
        now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        return {
            "weather.home": {
                "forecast": [
                    {"datetime": (now + timedelta(hours=i + 1)).isoformat(), "temperature": t, "cloud_coverage": 90}
                    for i, t in enumerate(temps)
                ]
            }
        }

    hass.services.async_register("weather", "get_forecasts", get_forecasts, supports_response=SupportsResponse.ONLY)


async def _config_flow(hass):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert r["step_id"] == "user"
    r = await hass.config_entries.flow.async_configure(
        r["flow_id"], {"name": "Drift", "main_climate": "climate.ac", "myzone_select": "select.ac_myzone", "weather": "weather.home"}
    )
    assert r["step_id"] == "zones"
    r = await hass.config_entries.flow.async_configure(
        r["flow_id"],
        {
            "tier1_zone": "climate.ac_nursery",
            "tier1_sensor": "sensor.nursery_temperature",
            "tier2_zones": ["climate.ac_master"],
            "tier3_zones": ["climate.ac_office", "climate.ac_lounge", "climate.ac_living"],
            "constant_zones": ["climate.ac_lounge", "climate.ac_living"],
        },
    )
    assert r["step_id"] == "energy"
    r = await hass.config_entries.flow.async_configure(
        r["flow_id"],
        {
            "grid_power": "sensor.grid_power",
            "grid_import_negative": True,
            "ac_phase_power": "sensor.grid_power_phase_b",
            "automations_to_disable": ["automation.nursery_too_hot"],
        },
    )
    assert r["type"] == "create_entry"
    await hass.async_block_till_done()
    return r["result"]


async def test_setup_shadow_mode_then_enable_cools_when_hot(hass: HomeAssistant, freezer):
    freezer.move_to("2026-01-15 21:00:00+11:00")
    await _setup_states(hass, nursery=26.4, outdoor=31, forecast_temps=[31, 30, 29, 28, 27, 26, 25, 24])
    set_mode = async_mock_service(hass, "climate", "set_hvac_mode")
    set_temp = async_mock_service(hass, "climate", "set_temperature")
    auto_off = async_mock_service(hass, "automation", "turn_off")

    entry = await _config_flow(hass)
    # the integration's own select platform registers select.select_option; mock it afterwards
    select = async_mock_service(hass, "select", "select_option")
    status = hass.states.get("sensor.drift_status")
    assert status is not None
    assert status.state == "disabled"
    assert "Shadow mode — would cool" in status.attributes["reason"]
    assert not set_mode  # shadow mode never touches the AC

    # entities exist
    for eid in [
        "switch.drift_control_enabled",
        "switch.drift_solar_pre_cool",
        "number.drift_tier_1_overnight_max",
        "time.drift_overnight_window_start",
        "binary_sensor.drift_overnight_window_active",
        "sensor.drift_tier_1_predicted_in_1h",
        "sensor.drift_ac_energy_from_grid",
        "sensor.drift_tuning_suggestions",
    ]:
        assert hass.states.get(eid) is not None, eid
    assert hass.states.get("number.drift_tier_1_overnight_max").state == "25.0"
    assert hass.states.get("time.drift_overnight_window_start").state == "17:30:00"
    assert hass.states.get("binary_sensor.drift_overnight_window_active").state == "on"

    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.drift_control_enabled"}, blocking=True)
    await hass.async_block_till_done()

    assert auto_off and auto_off[0].data["entity_id"] == ["automation.nursery_too_hot"]
    main_calls = [c for c in set_mode if c.data["entity_id"] == "climate.ac"]
    assert main_calls and main_calls[-1].data["hvac_mode"] == "cool"
    assert any(c.data == {"entity_id": "climate.ac_nursery", "temperature": 23.0} for c in set_temp)
    assert select and select[-1].data["option"] == "Nursery"
    st = hass.states.get("sensor.drift_status")
    assert st.state == "cooling", st.attributes["reason"]

    await hass.config_entries.async_unload(entry.entry_id)


async def test_tonight_scenario_holds_off(hass: HomeAssistant, freezer):
    freezer.move_to("2026-11-19 18:40:00+11:00")
    await _setup_states(hass, nursery=24.5, outdoor=25.0)
    set_mode = async_mock_service(hass, "climate", "set_hvac_mode")
    async_mock_service(hass, "automation", "turn_off")
    entry = await _config_flow(hass)
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.drift_control_enabled"}, blocking=True)
    await hass.async_block_till_done()
    st = hass.states.get("sensor.drift_status")
    assert st.state == "idle", st.attributes["reason"]
    assert not [c for c in set_mode if c.data["entity_id"] == "climate.ac"]
    pred = hass.states.get("sensor.drift_tier_1_predicted_in_3h")
    assert float(pred.state) < 24.5
    await hass.config_entries.async_unload(entry.entry_id)


async def test_manual_override_pauses(hass: HomeAssistant, freezer):
    freezer.move_to("2026-11-19 19:00:00+11:00")
    await _setup_states(hass, nursery=23.0, outdoor=22.0)
    async_mock_service(hass, "climate", "set_hvac_mode")
    async_mock_service(hass, "automation", "turn_off")
    entry = await _config_flow(hass)
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.drift_control_enabled"}, blocking=True)
    await hass.async_block_till_done()
    # someone turns the AC on by hand
    hass.states.async_set("climate.ac", "heat", {"current_temperature": 23, "temperature": 22, "hvac_modes": ["off", "cool", "heat"]})
    c = entry.runtime_data
    await c._async_control(dt_util.now() + timedelta(minutes=5))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.drift_status").state == "manual_override"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_options_flow_tuning(hass: HomeAssistant, freezer):
    freezer.move_to("2026-11-19 19:00:00+11:00")
    await _setup_states(hass)
    entry = await _config_flow(hass)
    r = await hass.config_entries.options.async_init(entry.entry_id)
    assert r["type"] == "menu"
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "tuning"})
    assert r["step_id"] == "tuning"
    data = {k: v for k, v in {
        "min_run_minutes": 40, "min_off_minutes": 20, "start_lag_minutes": 15, "cool_depth": 2.0, "heat_depth": 1.5,
        "tolerance_c": 0.3, "recovery_max_c": 0.7, "recovery_minutes": 45, "horizon_hours": 6, "min_open_zones": 3, "override_minutes": 120, "allow_cooling": True,
        "allow_heating": True, "precool_depth": 3.0, "precool_start_hour": 13, "precool_min_outdoor_c": 28,
        "precool_min_surplus_w": 2500, "import_tariff": 0.3, "feed_in_tariff": 0.05}.items()}
    r = await hass.config_entries.options.async_configure(r["flow_id"], data)
    assert r["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.runtime_data.cfg["cool_depth"] == 2.0
    await hass.config_entries.async_unload(entry.entry_id)


async def test_tog_select_sets_band_and_notifies(hass: HomeAssistant, freezer):
    freezer.move_to("2026-11-19 16:30:00+11:00")
    await _setup_states(hass, nursery=23.0, outdoor=24.0, forecast_temps=[24, 23, 22, 21, 20, 19, 18, 17, 16, 16, 15, 15, 15, 16, 17, 19])
    async_mock_service(hass, "climate", "set_hvac_mode")
    async_mock_service(hass, "climate", "set_temperature")
    notify = async_mock_service(hass, "notify", "mobile_app_test_phone")
    entry = await _config_flow(hass)

    await hass.services.async_call("select", "select_option", {"entity_id": "select.drift_tier_1_sleepwear_tog", "option": "1.0"}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get("number.drift_tier_1_overnight_min").state == "20.0"
    assert hass.states.get("number.drift_tier_1_overnight_max").state == "24.0"

    rec = hass.states.get("sensor.drift_tier_1_sleepwear_recommendation")
    assert rec.state in ("0.2", "1.0", "1.5", "2.5", "3.5"), rec.attributes

    # hand-editing the tier 1 band drops back to Manual
    await hass.services.async_call("number", "set_value", {"entity_id": "number.drift_tier_1_overnight_max", "value": 23.5}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get("select.drift_tier_1_sleepwear_tog").state == "Manual"

    # configure the bedtime notification via options, then reach 17:00
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "tog"})
    assert r["step_id"] == "tog"
    form = {"notify_service": "notify.mobile_app_test_phone", "notify_time": "17:00:00"}
    for k, (lo, hi) in {"0_2": (24, 27), "1_0": (20, 24), "1_5": (18, 22), "2_5": (16, 20), "3_5": (13, 16)}.items():
        form[f"tog_{k}_min"], form[f"tog_{k}_max"] = lo, hi
    r = await hass.config_entries.options.async_configure(r["flow_id"], form)
    assert r["type"] == "create_entry"
    await hass.async_block_till_done()

    entry = hass.config_entries.async_get_entry(entry.entry_id)
    c = entry.runtime_data
    freezer.move_to("2026-11-19 17:01:00+11:00")
    await c._async_control(dt_util.now())
    await c._async_sample(dt_util.now())
    await hass.async_block_till_done()
    assert len(notify) == 1
    assert "TOG tonight" in notify[0].data["message"]
    await c._async_sample(dt_util.now())
    assert len(notify) == 1  # once per day
    await hass.config_entries.async_unload(entry.entry_id)



async def _enabled_hot(hass, freezer):
    freezer.move_to("2026-01-15 21:00:00+11:00")
    await _setup_states(hass, nursery=26.4, outdoor=31, forecast_temps=[31, 30, 29, 28, 27, 26, 25, 24])
    set_mode = async_mock_service(hass, "climate", "set_hvac_mode")
    async_mock_service(hass, "climate", "set_temperature")
    async_mock_service(hass, "automation", "turn_off")
    pn_create = async_mock_service(hass, "persistent_notification", "create")
    pn_dismiss = async_mock_service(hass, "persistent_notification", "dismiss")
    entry = await _config_flow(hass)
    async_mock_service(hass, "select", "select_option")
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.drift_control_enabled"}, blocking=True)
    await hass.async_block_till_done()
    return entry, set_mode, pn_create, pn_dismiss


def _main_cmds(calls):
    return [c.data["hvac_mode"] for c in calls if c.data["entity_id"] == "climate.ac"]


async def test_dropped_command_is_retried_then_reported_not_treated_as_override(hass, freezer):
    entry, set_mode, pn_create, _ = await _enabled_hot(hass, freezer)
    c = entry.runtime_data
    assert _main_cmds(set_mode) == ["cool"]
    # MyAir never applies it: climate.ac stays "off"
    for _ in range(4):
        freezer.tick(timedelta(seconds=95))
        await c._async_sample(dt_util.now())
    assert _main_cmds(set_mode) == ["cool", "cool", "cool"]
    assert pn_create and "hasn't responded" in pn_create[0].data["message"]
    assert "Nursery is 26.4" in pn_create[0].data["message"]
    freezer.tick(timedelta(minutes=5))
    await c._async_control(dt_util.now())
    st = hass.states.get("sensor.drift_status")
    assert st.state == "ac_unreachable", st.attributes["reason"]
    assert c.override_until is None
    assert _main_cmds(set_mode)[-1] == "cool"  # still trying every cycle
    # it finally lands: the alert clears
    hass.states.async_set("climate.ac", "cool", {"current_temperature": 26, "temperature": 23.5, "hvac_modes": ["off", "cool", "heat"]})
    freezer.tick(timedelta(seconds=60))
    await c._async_sample(dt_util.now())
    await c._async_control(dt_util.now())
    assert c.pending is None and c.fault is None
    assert hass.states.get("sensor.drift_status").state == "cooling"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_myair_outage_and_return_in_wrong_state_is_resent_not_an_override(hass, freezer):
    entry, set_mode, pn_create, pn_dismiss = await _enabled_hot(hass, freezer)
    c = entry.runtime_data
    hass.states.async_set("climate.ac", "cool", {"current_temperature": 26, "temperature": 23.5, "hvac_modes": ["off", "cool", "heat"]})
    freezer.tick(timedelta(seconds=60))
    await c._async_sample(dt_util.now())
    assert c.pending is None
    # MyAir drops off for 12 minutes
    hass.states.async_set("climate.ac", "unavailable", {})
    for _ in range(12):
        freezer.tick(timedelta(seconds=60))
        await c._async_sample(dt_util.now())
    assert c.ac_mode_actual == "cool"  # held, not recorded as a stop
    assert pn_create and "unavailable since" in pn_create[0].data["message"]
    # ...and comes back reporting "off"
    hass.states.async_set("climate.ac", "off", {"current_temperature": 26.2, "temperature": 23.5, "hvac_modes": ["off", "cool", "heat"]})
    freezer.tick(timedelta(seconds=60))
    await c._async_sample(dt_util.now())
    await c._async_control(dt_util.now())
    assert c.override_until is None
    assert _main_cmds(set_mode)[-1] == "cool"
    assert pn_dismiss
    await hass.config_entries.async_unload(entry.entry_id)


async def test_tier1_leaving_band_triggers_an_immediate_check(hass, freezer):
    freezer.move_to("2026-01-15 21:00:00+11:00")
    await _setup_states(hass, nursery=24.0, outdoor=24, forecast_temps=[24, 23, 22, 21, 20, 19, 18, 18])
    set_mode = async_mock_service(hass, "climate", "set_hvac_mode")
    async_mock_service(hass, "climate", "set_temperature")
    async_mock_service(hass, "automation", "turn_off")
    async_mock_service(hass, "persistent_notification", "create")
    entry = await _config_flow(hass)
    async_mock_service(hass, "select", "select_option")
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.drift_control_enabled"}, blocking=True)
    await hass.async_block_till_done()
    assert not _main_cmds(set_mode)
    freezer.tick(timedelta(minutes=3))
    hass.states.async_set("sensor.nursery_temperature", "26.3", {"device_class": "temperature", "unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert _main_cmds(set_mode) == ["cool"]  # hard limit, acted on without waiting for the 5-minute cycle
    await hass.config_entries.async_unload(entry.entry_id)




async def test_constant_zones_are_never_closed_when_running(hass, freezer):
    entry, set_mode, _, _ = await _enabled_hot(hass, freezer)
    closed = [c.data["entity_id"] for c in set_mode if c.data.get("hvac_mode") == "off" and c.data["entity_id"] != "climate.ac"]
    assert "climate.ac_office" in closed  # an ordinary tier 3 zone is closed
    assert "climate.ac_lounge" not in closed and "climate.ac_living" not in closed
    await hass.config_entries.async_unload(entry.entry_id)


async def test_outdoor_sensor_overrides_model_and_falls_back_when_stale(hass, freezer):
    freezer.move_to("2026-11-20 17:00:00+11:00")
    await _setup_states(hass, nursery=24.4, outdoor=24.3)
    hass.states.async_set("sensor.bom_station_temp", "19.0", {"device_class": "temperature", "unit_of_measurement": "°C"})
    async_mock_service(hass, "automation", "turn_off")
    entry = await _config_flow(hass)

    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "zones"})
    r = await hass.config_entries.options.async_configure(
        r["flow_id"],
        {
            "tier1_zone": "climate.ac_nursery",
            "tier1_sensor": "sensor.nursery_temperature",
            "outdoor_sensor": "sensor.bom_station_temp",
            "tier2_zones": ["climate.ac_master"],
            "tier3_zones": ["climate.ac_office", "climate.ac_lounge", "climate.ac_living"],
            "constant_zones": ["climate.ac_lounge", "climate.ac_living"],
        },
    )
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(entry.entry_id)
    ctrl = entry.runtime_data
    now = dt_util.now()
    ctrl._read_weather()
    assert ctrl.outdoor == 19.0
    assert ctrl.outdoor_source == "sensor.bom_station_temp"

    # the 5.3°C model error is carried into the next hours and gone by 3 h
    raw = {p.when: p.temp for p in ctrl.forecast}
    adj = {p.when: p.temp for p in ctrl.planning_forecast(now)}
    first, *_ = sorted(raw)
    assert adj[first] < raw[first] - 2
    late = [w for w in raw if (w - now).total_seconds() >= 3 * 3600]
    assert late and all(adj[w] == raw[w] for w in late)

    # observation goes quiet: fall back to the weather entity rather than trust a stale reading
    freezer.tick(timedelta(minutes=91))
    ctrl._read_weather()
    assert ctrl.outdoor == 24.3
    assert ctrl.outdoor_source == "weather.home"
    assert ctrl.planning_forecast(dt_util.now()) is ctrl.forecast
    await hass.config_entries.async_unload(entry.entry_id)


async def test_manual_pause_and_last_stop_survive_a_reload(hass, freezer):
    """2026-11-20: a restart and an options reload wiped the pause and Drift restarted the AC."""
    freezer.move_to("2026-11-20 17:10:00+11:00")
    await _setup_states(hass, nursery=26.4, outdoor=31, forecast_temps=[31, 30, 29, 28, 27, 26, 25, 24])
    set_mode = async_mock_service(hass, "climate", "set_hvac_mode")
    async_mock_service(hass, "climate", "set_temperature")
    async_mock_service(hass, "automation", "turn_off")
    entry = await _config_flow(hass)
    async_mock_service(hass, "select", "select_option")
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.drift_control_enabled"}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.drift_status").state == "cooling"
    hass.states.async_set("climate.ac", "cool", {"current_temperature": 26.4, "temperature": 22, "hvac_modes": ["off", "cool"]})
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=2))
    async_fire_time_changed(hass)  # the per-minute check sees the command landed
    await hass.async_block_till_done()
    assert entry.runtime_data.pending is None

    # someone stops it at the panel
    freezer.tick(timedelta(minutes=6))
    hass.states.async_set("climate.ac", "off", {"current_temperature": 26.3, "temperature": 22, "hvac_modes": ["off", "cool"]})
    await hass.async_block_till_done()
    await entry.runtime_data._async_control(dt_util.now())
    await hass.async_block_till_done()
    assert hass.states.get("sensor.drift_status").state == "manual_override"
    until = entry.runtime_data.override_until

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    c = entry.runtime_data
    assert c.override_until == until
    assert c.last_stop is not None
    set_mode.clear()
    await c._async_control(dt_util.now() + timedelta(minutes=1))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.drift_status").state == "manual_override"
    assert not [x for x in set_mode if x.data["entity_id"] == "climate.ac"]
    await hass.config_entries.async_unload(entry.entry_id)


# --- IDEAS 13: restart and handover safety ---------------------------------------------------

AC_COOL = {"current_temperature": 26, "temperature": 23.5, "hvac_modes": ["off", "cool", "heat"]}
AC_OFF = {"current_temperature": 26, "temperature": 23.5, "hvac_modes": ["off", "cool", "heat"]}


async def _restart(hass, entry, mutate=None, ac_state=None):
    """Stop Drift, optionally rewrite its saved state or the AC's state, and start it again."""
    from custom_components.drift.const import STORAGE_KEY, STORAGE_VERSION
    from homeassistant.helpers.storage import Store

    await hass.config_entries.async_unload(entry.entry_id)
    if mutate:
        store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}")
        data = await store.async_load()
        mutate(data)
        await store.async_save(data)
    if ac_state:
        hass.states.async_set("climate.ac", *ac_state)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


async def _land(hass, freezer, c, mode="cool"):
    """The AC reports the mode Drift sent, and the per-minute check sees it land."""
    hass.states.async_set("climate.ac", mode, AC_COOL)
    freezer.tick(timedelta(seconds=60))
    await c._async_sample(dt_util.now())
    assert c.pending is None


async def test_inherited_run_is_not_held_after_restart(hass, freezer):
    freezer.move_to("2026-11-20 19:00:00+11:00")
    await _setup_states(hass, nursery=22.0, outdoor=19.0, forecast_temps=[19, 18, 18, 17, 17, 16, 16, 16])
    for n in ("master", "office", "lounge", "living"):
        _zone(hass, n, 22.0)
    set_mode = async_mock_service(hass, "climate", "set_hvac_mode")
    async_mock_service(hass, "climate", "set_temperature")
    async_mock_service(hass, "automation", "turn_off")
    entry = await _config_flow(hass)
    async_mock_service(hass, "select", "select_option")
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.drift_control_enabled"}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.drift_status").state == "idle"

    # Someone (or an older Drift) left the AC running; the saved state predates run_is_ours/expected_mode.
    def older_version(data):
        data.pop("run_is_ours", None)
        data.pop("expected_mode", None)

    set_mode.clear()
    hass.states.async_set("climate.ac", "cool", AC_COOL)
    c = await _restart(hass, entry, older_version)
    assert c.run_is_ours is False
    await c._async_control(dt_util.now())
    await hass.async_block_till_done()
    assert "off" in _main_cmds(set_mode)
    assert hass.states.get("sensor.drift_status").state == "idle", c.reason
    await hass.config_entries.async_unload(entry.entry_id)


async def test_drift_run_is_held_across_restart(hass, freezer):
    entry, set_mode, _, _ = await _enabled_hot(hass, freezer)
    c = entry.runtime_data
    await _land(hass, freezer, c)
    assert c.run_is_ours is True
    freezer.tick(timedelta(minutes=4))  # five minutes into the run

    c = await _restart(hass, entry)
    assert c.run_is_ours is True
    set_mode.clear()
    await c._async_control(dt_util.now())
    await hass.async_block_till_done()
    st = hass.states.get("sensor.drift_status")
    assert st.state == "cooling" and "minimum run" in st.attributes["reason"], st.attributes["reason"]
    assert "off" not in _main_cmds(set_mode)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_manual_change_right_after_restart_is_respected(hass, freezer):
    entry, set_mode, _, _ = await _enabled_hot(hass, freezer)
    c = entry.runtime_data
    await _land(hass, freezer, c)

    # restart with the AC entity not loaded yet, then it appears as it was left
    c = await _restart(hass, entry, ac_state=("unavailable", {}))
    assert c.ac_unavailable_since is None and c._awaiting_first_state
    hass.states.async_set("climate.ac", "cool", AC_COOL)
    freezer.tick(timedelta(seconds=30))
    await c._async_sample(dt_util.now())
    assert not c._awaiting_first_state and c.ac_back_at is None  # no "recently back" grace
    assert c.expected_mode == "cool"

    # two minutes later a person turns it off
    freezer.tick(timedelta(minutes=2))
    hass.states.async_set("climate.ac", "off", AC_OFF)
    set_mode.clear()
    await c._async_control(dt_util.now())
    await hass.async_block_till_done()
    assert hass.states.get("sensor.drift_status").state == "manual_override"
    assert "cool" not in _main_cmds(set_mode)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_first_state_after_restart_that_differs_is_the_baseline(hass, freezer):
    """If the AC isn't as we left it when it first reports, that is the starting point, not an override."""
    entry, set_mode, _, _ = await _enabled_hot(hass, freezer)
    c = entry.runtime_data
    await _land(hass, freezer, c)
    c = await _restart(hass, entry, ac_state=("unavailable", {}))
    hass.states.async_set("climate.ac", "off", AC_OFF)
    await c._async_sample(dt_util.now())
    assert c.expected_mode == "off" and c.ac_back_at is None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_real_outage_still_gets_grace(hass, freezer):
    entry, set_mode, _, _ = await _enabled_hot(hass, freezer)
    c = entry.runtime_data
    await _land(hass, freezer, c)
    hass.states.async_set("climate.ac", "unavailable", {})
    for _ in range(3):
        freezer.tick(timedelta(seconds=60))
        await c._async_sample(dt_util.now())
    assert c.ac_unavailable_since is not None
    hass.states.async_set("climate.ac", "off", AC_OFF)
    freezer.tick(timedelta(seconds=60))
    await c._async_sample(dt_util.now())
    assert c.ac_back_at is not None
    await c._async_control(dt_util.now())
    assert c.override_until is None
    assert _main_cmds(set_mode)[-1] == "cool"  # resent, not respected as a manual stop
    await hass.config_entries.async_unload(entry.entry_id)


async def test_change_after_command_landed_is_manual(hass, freezer):
    entry, set_mode, _, _ = await _enabled_hot(hass, freezer)
    c = entry.runtime_data
    await _land(hass, freezer, c)
    assert c._landed_at is not None
    freezer.tick(timedelta(seconds=60))  # still inside the 3-minute echo allowance
    hass.states.async_set("climate.ac", "off", AC_OFF)
    set_mode.clear()
    await c._async_control(dt_util.now())
    await hass.async_block_till_done()
    assert hass.states.get("sensor.drift_status").state == "manual_override"
    assert c.override_until is not None
    assert "cool" not in _main_cmds(set_mode)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_slow_echo_is_not_an_override(hass, freezer):
    entry, set_mode, _, _ = await _enabled_hot(hass, freezer)
    c = entry.runtime_data
    for _ in range(2):  # the AC still says "off" 90+ seconds after the command
        freezer.tick(timedelta(seconds=50))
        await c._async_sample(dt_util.now())
    assert c.pending is not None and c._landed_at is None
    await c._async_control(dt_util.now())
    assert c.override_until is None
    hass.states.async_set("climate.ac", "cool", AC_COOL)  # the echo finally arrives
    freezer.tick(timedelta(seconds=30))
    await c._async_sample(dt_util.now())
    await c._async_control(dt_util.now())
    assert c.override_until is None
    assert hass.states.get("sensor.drift_status").state == "cooling"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_ac_since_and_last_stop_are_local_time(hass, freezer):
    """main.last_changed is UTC; reasons like 'minimum run until 18:14' must read in local time."""
    freezer.move_to("2026-11-20 19:00:00+11:00")  # 08:00 UTC
    await _setup_states(hass, nursery=26.4, outdoor=31, forecast_temps=[31, 30, 29, 28, 27, 26, 25, 24])
    hass.states.async_set("climate.ac", "cool", AC_COOL)
    async_mock_service(hass, "climate", "set_hvac_mode")
    async_mock_service(hass, "climate", "set_temperature")
    async_mock_service(hass, "automation", "turn_off")
    entry = await _config_flow(hass)
    c = entry.runtime_data
    assert c.ac_since.utcoffset() == timedelta(hours=11) and c.ac_since.hour == 19

    def saved_in_utc(data):
        data["last_stop"] = "2026-11-20T07:00:00+00:00"
        data["override_until"] = "2026-11-20T09:00:00+00:00"

    c = await _restart(hass, entry, saved_in_utc)
    assert c.last_stop.utcoffset() == timedelta(hours=11) and c.last_stop.hour == 18
    assert c.override_until.utcoffset() == timedelta(hours=11) and c.override_until.hour == 20
    await hass.config_entries.async_unload(entry.entry_id)


async def _enable_with_models(hass, freezer, forecast_temps, outdoor, trained=True):
    """Hot-room set-up (hard limit, so Drift cools at once), with a chosen forecast and trained models."""
    freezer.move_to("2026-01-15 21:00:00+11:00")
    await _setup_states(hass, nursery=26.4, outdoor=outdoor, forecast_temps=forecast_temps)
    for n, t in [("nursery", 26.4), ("master", 24.4), ("office", 24.4), ("lounge", 23.6), ("living", 23.7)]:
        _zone(hass, n, t, setpoint=30)  # nothing already matches what Drift will send
    set_mode = async_mock_service(hass, "climate", "set_hvac_mode")
    set_temp = async_mock_service(hass, "climate", "set_temperature")
    async_mock_service(hass, "automation", "turn_off")
    entry = await _config_flow(hass)
    async_mock_service(hass, "select", "select_option")
    c = entry.runtime_data
    if trained:
        for m in c.models.values():
            m.n_passive = 100
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.drift_control_enabled"}, blocking=True)
    await hass.async_block_till_done()
    return entry, c, set_mode, set_temp


def _setpoints(set_temp, entity):
    return [x.data["temperature"] for x in set_temp if x.data["entity_id"] == entity]


async def test_whole_degree_setpoint_is_not_resent_each_cycle(hass, freezer):
    entry, c, set_mode, set_temp = await _enable_with_models(hass, freezer, [31, 30, 29, 28, 27, 26, 25, 24], 31, trained=False)
    assert _setpoints(set_temp, "climate.ac_nursery") == [23.0]  # 23.5 target, sent as a whole degree
    assert all(float(v).is_integer() for x in set_temp for v in [x.data["temperature"]])
    # the AC applies exactly what it was sent
    for x in set_temp:
        st = hass.states.get(x.data["entity_id"])
        hass.states.async_set(x.data["entity_id"], st.state, {**st.attributes, "temperature": x.data["temperature"]})
    hass.states.async_set("climate.ac", "cool", {**hass.states.get("climate.ac").attributes, "temperature": 23.0})
    sent = len(set_temp)
    for _ in range(3):
        freezer.tick(timedelta(minutes=5))
        await c._async_sample(dt_util.now())
        await c._async_control(dt_util.now())
    assert len(set_temp) == sent, [(x.data["entity_id"], x.data["temperature"]) for x in set_temp[sent:]]
    await hass.config_entries.async_unload(entry.entry_id)


async def test_cold_night_cool_run_uses_the_shallow_setpoint_and_stops_there(hass, freezer):
    entry, c, set_mode, set_temp = await _enable_with_models(hass, freezer, [10, 9, 9, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8], 10)
    # fixed depth would be 25 - 1.5 = 23.5 -> 23; the cold night lets it stop at 24.5 -> 24
    assert _setpoints(set_temp, "climate.ac_nursery") == [24.0]
    assert _setpoints(set_temp, "climate.ac") == [24.0]
    assert c.decision.zone_commands["climate.ac_nursery"].target == 24.5

    # the AC reports it, runs the minimum, and the room reaches the shallow target
    hass.states.async_set("climate.ac", "cool", {"current_temperature": 26, "temperature": 24, "hvac_modes": ["off", "cool", "heat"]})
    freezer.tick(timedelta(seconds=60))
    await c._async_sample(dt_util.now())
    assert c.pending is None
    freezer.tick(timedelta(minutes=31))
    for n in ("nursery", "master", "office", "lounge", "living"):
        st = hass.states.get(f"climate.ac_{n}")
        hass.states.async_set(f"climate.ac_{n}", st.state, {**st.attributes, "current_temperature": 24.4})
    hass.states.async_set("sensor.nursery_temperature", "24.4", {"device_class": "temperature", "unit_of_measurement": "°C"})
    await c._async_sample(dt_util.now())
    await c._async_control(dt_util.now())
    st = hass.states.get("sensor.drift_status")
    assert "reached target" in st.attributes["reason"], st.attributes["reason"]
    await hass.config_entries.async_unload(entry.entry_id)


async def test_own_run_is_restored_when_the_ac_loads_after_a_restart(hass, freezer):
    entry, set_mode, _, _ = await _enabled_hot(hass, freezer)
    c = entry.runtime_data
    await _land(hass, freezer, c)
    started = c.ac_since
    starts = c.totals.starts
    freezer.tick(timedelta(minutes=4))

    # Home Assistant restarts and the AC entity isn't there yet when Drift starts
    c = await _restart(hass, entry, ac_state=("unavailable", {}))
    assert c._awaiting_first_state
    freezer.tick(timedelta(minutes=1))
    hass.states.async_set("climate.ac", "cool", AC_COOL)  # it loads, still running
    await c._async_sample(dt_util.now())
    assert c.run_is_ours is True
    assert c.ac_since == started  # the run's true start, not "now"
    assert c.totals.starts == starts  # the same run, not a new start
    set_mode.clear()
    await c._async_control(dt_util.now())
    await hass.async_block_till_done()
    st = hass.states.get("sensor.drift_status")
    assert st.state == "cooling" and "minimum run" in st.attributes["reason"], st.attributes["reason"]
    assert "off" not in _main_cmds(set_mode)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_nothing_is_sent_while_the_ac_has_not_reported_since_a_restart(hass, freezer):
    entry, set_mode, _, _ = await _enabled_hot(hass, freezer)
    # (Drift's cool hadn't landed before the restart: forget it, so this isn't read as a person's change)
    c = await _restart(hass, entry, mutate=lambda d: d.update(expected_mode="off"), ac_state=("unavailable", {}))
    set_mode = async_mock_service(hass, "climate", "set_hvac_mode")
    set_temp = async_mock_service(hass, "climate", "set_temperature")
    await c._async_control(dt_util.now())
    await hass.async_block_till_done()
    assert not set_mode and not set_temp  # the planner wants to cool, but the AC's state is unknown
    assert hass.states.get("sensor.drift_status").state == "waiting_for_ac"
    # once it reports, control resumes
    hass.states.async_set("climate.ac", "off", AC_OFF)
    await c._async_sample(dt_util.now())
    await c._async_control(dt_util.now())
    await hass.async_block_till_done()
    assert "cool" in _main_cmds(set_mode)
    await hass.config_entries.async_unload(entry.entry_id)
