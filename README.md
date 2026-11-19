# drift

Forecast-aware, tiered climate control for zoned ducted AC in Home Assistant, built and tested against Advantage Air / MyAir. All logic runs locally in Home Assistant; only the weather forecast comes from the internet.

Status: unreleased. Installed and running in shadow mode.

## What it does

- **Looks ahead.** Every 5 minutes it projects each room's temperature for the next 6 hours with the AC off, using a learned thermal model per room and your hourly forecast.
- **Lets rooms cool or warm naturally when they can.** It only starts the AC at the latest moment that still keeps the room in band. A room that is already a little outside its band (up to 0.7 °C) is left alone if the forecast has it back inside within 45 minutes on its own. Beyond 1 °C it always acts.
- **Is ready on time.** It pre-cools or pre-heats before the overnight window starts, so the priority room is already in band at bedtime.
- **Prevents short-cycling.** It runs in proper blocks, with a minimum run time, a minimum off time, and a target set below the band edge.
- **Uses tiers to decide which rooms get air:**
  - Tier 1 (e.g. a child's bedroom) is held tightly in band, and its zone is the MyZone.
  - Tier 2 (e.g. the main bedroom) gets air when it's drifting.
  - Tier 3 (the rest) stays shut unless it's near the edge of its wide band, or needed to keep a minimum number of zones open for the ducts.
- **Sleepwear (TOG) mode.** Pick the TOG of the sleep suit or bag being worn tonight, and the tier 1 band follows the chart. At a set time each afternoon it suggests the TOG that suits the room's natural temperature tonight, which means the least AC work.
- **Solar pre-cool.** On hot afternoons with spare solar, it cools the house harder before evening.
- **Tracks energy.** It learns the AC's power draw from the step change on a grid meter each time the AC starts or stops. It then splits the AC's energy into solar and grid.
- **Reviews each night.** It records the time in band, compressor starts, runtime, energy, the solar/grid split, cost, forecast accuracy and the TOG in use. It also suggests tuning changes, but never applies them on its own.
- **Respects manual changes.** If someone changes the AC by hand, it pauses for 2 hours (adjustable).
- **Has a shadow mode.** While switched off, it still plans and shows what it *would* do, without touching anything.

## Install

**Marketplace (HACS):** go to ⋮ → Custom repositories, add `Peytonizer/drift` as type *Integration*, install, and restart Home Assistant.
**Manual:** copy `custom_components/drift` into `/config/custom_components/` and restart Home Assistant.

Then go to **Settings → Devices & services → Add integration → Drift**.

| Step | Field | Example |
|---|---|---|
| System | Main AC unit | `climate.ac` |
| | MyZone selector | `select.ac_myzone` |
| | Weather (hourly forecast) | `weather.home` |
| Zones | Tier 1 zone (+ optional room sensor) | `climate.ac_bedroom_1` / `sensor.bedroom_1_temperature` |
| | Tier 2 zones | `climate.ac_main_bedroom` |
| | Tier 3 zones | everything else |
| Energy | Grid meter power (whole house) | e.g. a SolarEdge meter `…_ac_power` (import negative) |
| | Meter phase the AC is on | e.g. `…_ac_power_b` |
| | Automations to pause | any existing AC automations it replaces |

It starts **disabled (shadow mode)**. Watch `sensor.drift_status` → `reason`, then turn on `switch.drift_control_enabled`. Switching it on turns the listed automations off, and switching it off turns them back on.

## Changing things without redeploying

| What | Where |
|---|---|
| Tier 1 sleepwear TOG (sets the tier 1 band) | `select.drift_tier_1_sleepwear_tog` |
| Bands: tier 1/2/3 overnight, daytime guard | Number entities on the device page (editing tier 1 by hand switches TOG to *Manual*) |
| Overnight window | Time entities (default 17:30 → 07:00) |
| Control on/off, solar pre-cool | Switches |
| Run/off times, depths, tolerance, natural recovery, tariffs, heating/cooling allowed | Configure → Tuning |
| TOG chart, bedtime notification | Configure → Sleepwear (TOG) |
| Zone tiers, sensors, meter | Configure → Zones / Energy |

### Default TOG chart (editable)

| TOG | Band | Aims for when running (default depths) |
|---|---|---|
| 0.2 | 24–27 °C | cool to 25.5, heat to 25.5 |
| 1.0 | 20–24 °C | cool to 22.5, heat to 21.5 |
| 1.5 | 18–22 °C | cool to 20.5, heat to 19.5 |
| 2.5 | 16–20 °C | cool to 18.5, heat to 17.5 |
| 3.5 | below 16 °C (13–16 here) | cool to 14.5, heat to 14.5 |

The defaults follow Love to Dream's Australian ratings. Brands differ, so edit the chart to match your sleepwear. The chart range is the band itself; the cool and heat depths (Tuning) decide where the AC aims inside it, so the room has the full range to drift in.

## Entities

| Entity | What it is |
|---|---|
| `sensor.drift_status` | idle / cooling / heating / precooling / manual_override / disabled. Attributes: reason, MyZone, next start, per-zone temperature, band and command. |
| `sensor.drift_tier_1_sleepwear_recommendation` | Recommended TOG for tonight, with the room's natural overnight range. |
| `sensor.drift_tier_1_predicted_in_1h` / `_3h` | Projection with the AC off. |
| `sensor.drift_ac_power_estimated`, `…_ac_energy*` | Power and energy. The energy sensors work with the Energy dashboard. |
| `sensor.drift_last_night_tier_1_in_band` | Last night's summary. |
| `sensor.drift_tuning_suggestions` | Suggestions plus 7-night totals. |
| `sensor.drift_thermal_model_samples` | What the model has learned for each room (diagnostic). |

## Notes

- The model learns online. Expect predictions to firm up after 2–3 days.
- AC power is estimated from meter steps. A CT clamp on the AC circuit would be more precise.
- Heating is allowed by default. Turn off *Allow heating* in Tuning to stop it heating on cool summer nights.

## Tests

`pip install pytest-homeassistant-custom-component && pytest`
