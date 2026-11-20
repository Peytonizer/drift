"""Comfort rules end-to-end: subentry flow, a real schedule helper, presence sensors."""
from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_mock_service

from tests.test_integration import _config_flow, _setup_states

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


async def _schedules(hass):
    assert await async_setup_component(
        hass,
        "schedule",
        {
            "schedule": {
                "drift_office": {"name": "Drift office", **{d: [{"from": "08:00:00", "to": "16:30:00"}] for d in WEEKDAYS}},
                "drift_naps": {"name": "Drift naps", "monday": [{"from": "07:00:00", "to": "17:30:00"}]},
            }
        },
    )
    await hass.async_block_till_done()


async def _add_rule(hass, entry, data):
    r = await hass.config_entries.subentries.async_init((entry.entry_id, "comfort_rule"), context={"source": "user"})
    assert r["type"] == "form", r
    r = await hass.config_entries.subentries.async_configure(r["flow_id"], data)
    await hass.async_block_till_done()
    return r


def _band(hass, zone):
    return list(hass.states.get("sensor.drift_status").attributes["zones"][zone]["band"])


async def test_rules_via_subentries_with_a_real_schedule(hass: HomeAssistant, freezer):
    freezer.move_to("2026-11-23 09:00:00+11:00")  # Monday
    await _setup_states(hass, nursery=23.0, outdoor=22)
    await _schedules(hass)
    async_mock_service(hass, "climate", "set_hvac_mode")
    entry = await _config_flow(hass)

    # validation: a schedule rule needs its helper
    r = await hass.config_entries.subentries.async_init((entry.entry_id, "comfort_rule"), context={"source": "user"})
    r = await hass.config_entries.subentries.async_configure(
        r["flow_id"], {"name": "Office", "zones": ["climate.ac_office"], "trigger": "schedule", "band": "fixed", "min": 21, "max": 25}
    )
    assert r["type"] == "form" and r["errors"] == {"schedule": "schedule_required"}
    r = await hass.config_entries.subentries.async_configure(
        r["flow_id"],
        {"name": "Office", "zones": ["climate.ac_office"], "trigger": "schedule", "schedule": "schedule.drift_office", "band": "fixed", "min": 21, "max": 25},
    )
    assert r["type"] == "create_entry"
    await hass.async_block_till_done()
    r = await _add_rule(
        hass,
        entry,
        {"name": "Nursery naps", "zones": ["climate.ac_nursery"], "trigger": "schedule", "schedule": "schedule.drift_naps", "band": "tier1"},
    )
    assert r["type"] == "create_entry"

    entry = hass.config_entries.async_get_entry(entry.entry_id)
    c = entry.runtime_data
    assert {x.name for x in c.rules} == {"Office", "Nursery naps"}  # reloaded with the new rules
    await c._async_control(dt_util.now())
    st = hass.states.get("sensor.drift_status")
    assert set(st.attributes["active_rules"]) == {"Office", "Nursery naps"}
    assert _band(hass, "Office") == [21.0, 25.0]
    assert _band(hass, "Lounge") == [18.0, 26.0]  # no rule: daytime guard
    assert _band(hass, "Nursery") == [21.0, 25.0]  # tier 1 overnight band (Manual)

    # the nap rule follows the TOG
    await hass.services.async_call("select", "select_option", {"entity_id": "select.drift_tier_1_sleepwear_tog", "option": "1.0"}, blocking=True)
    await hass.async_block_till_done()
    assert _band(hass, "Nursery") == [20.0, 24.0]

    # Tuesday: office yes, naps no
    freezer.move_to("2026-11-24 09:00:00+11:00")
    await c._async_control(dt_util.now())
    assert hass.states.get("sensor.drift_status").attributes["active_rules"] == ["Office"]
    assert _band(hass, "Nursery") == [18.0, 26.0]

    # deleting the nap rule removes it
    nap_id = next(sid for sid, s in entry.subentries.items() if s.title == "Nursery naps")
    assert hass.config_entries.async_remove_subentry(entry, nap_id)
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(entry.entry_id)
    assert [x.name for x in entry.runtime_data.rules] == ["Office"]
    await hass.config_entries.async_unload(entry.entry_id)


async def test_weekend_presence_rule(hass: HomeAssistant, freezer):
    freezer.move_to("2026-11-21 10:00:00+11:00")  # Saturday
    await _setup_states(hass, nursery=23.0, outdoor=22)
    hass.states.async_set("binary_sensor.lounge_presence", "off")
    async_mock_service(hass, "climate", "set_hvac_mode")
    entry = await _config_flow(hass)
    await _add_rule(
        hass,
        entry,
        {
            "name": "Weekend living",
            "zones": ["climate.ac_lounge", "climate.ac_living"],
            "trigger": "presence",
            "presence": ["binary_sensor.lounge_presence"],
            "presence_days": ["saturday", "sunday"],
            "dwell_minutes": 5,
            "hold_minutes": 30,
            "band": "fixed",
            "min": 21,
            "max": 25,
        },
    )
    entry = hass.config_entries.async_get_entry(entry.entry_id)
    c = entry.runtime_data
    hass.states.async_set("binary_sensor.lounge_presence", "on")  # someone walks in
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=3))
    await c._async_sample(dt_util.now())
    await c._async_control(dt_util.now())
    assert _band(hass, "Lounge") == [18.0, 26.0]  # 3 minutes isn't a stay yet
    freezer.tick(timedelta(minutes=3))
    await c._async_sample(dt_util.now())
    await c._async_control(dt_util.now())
    assert _band(hass, "Lounge") == [21.0, 25.0]
    assert _band(hass, "Living") == [21.0, 25.0]
    hass.states.async_set("binary_sensor.lounge_presence", "off")
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=20))
    await c._async_sample(dt_util.now())
    await c._async_control(dt_util.now())
    assert _band(hass, "Lounge") == [21.0, 25.0]  # held 30 minutes after they leave
    freezer.tick(timedelta(minutes=15))
    await c._async_sample(dt_util.now())
    await c._async_control(dt_util.now())
    assert _band(hass, "Lounge") == [18.0, 26.0]
    await hass.config_entries.async_unload(entry.entry_id)
