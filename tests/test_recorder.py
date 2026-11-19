"""The history-training path against a real recorder (needs the recorder set up before hass)."""
from __future__ import annotations

from datetime import timedelta

from tests.test_integration import _config_flow, _setup_states

async def test_relearn_reads_real_recorder_history(recorder_mock, enable_custom_integrations, hass, freezer):
    """Exercise the recorder query and the conversion into the fitter with real rows."""
    from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

    freezer.move_to("2026-11-18 18:00:00+11:00")
    await _setup_states(hass, nursery=24.8, outdoor=24.0)
    room = 24.8
    for i in range(16 * 12):  # 16 hours at 5-minute steps, AC off, room easing down overnight
        freezer.tick(timedelta(minutes=5))
        room -= 0.025
        hass.states.async_set("sensor.nursery_temperature", f"{room:.1f}", {"device_class": "temperature", "unit_of_measurement": "°C"})
        if i % 12 == 0:
            hass.states.async_set("weather.home", "cloudy", {"temperature": 20 - i / 24, "cloud_coverage": 60})
        hass.states.async_set("climate.ac", "off", {"current_temperature": room, "temperature": 22, "hvac_modes": ["off", "cool", "heat"]})
    await async_wait_recording_done(hass)
    entry = await _config_flow(hass)
    await hass.async_block_till_done()
    c = entry.runtime_data
    info = c.bootstrap_info
    assert info and "error" not in info, info
    assert info["zones"]["Nursery"]["passive"] >= 50 and info["zones"]["Nursery"]["used"]
    model = hass.states.get("sensor.drift_thermal_model_samples")
    assert model.attributes["trained_from_history"]["zones"]["Nursery"]["used"]
    await hass.config_entries.async_unload(entry.entry_id)
