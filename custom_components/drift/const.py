"""Constants for Drift."""
from __future__ import annotations

DOMAIN = "drift"
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.state"

PLATFORMS = ["binary_sensor", "button", "number", "select", "sensor", "switch", "time"]

# --- config keys (setup / zones / energy) ---
CONF_NAME = "name"
CONF_MAIN_CLIMATE = "main_climate"
CONF_MYZONE_SELECT = "myzone_select"
CONF_WEATHER = "weather"
CONF_TIER1_ZONE = "tier1_zone"
CONF_TIER1_SENSOR = "tier1_sensor"
CONF_TIER1_DOOR_SENSOR = "tier1_door_sensor"
CONF_OUTDOOR_SENSOR = "outdoor_sensor"
# A real observation older than this is ignored in favour of the weather entity's model value.
# BoM AWS stations report every 30 min (sometimes 10), so 90 min allows two missed reports.
OUTDOOR_SENSOR_MAX_AGE_MIN = 90
# How long a model-vs-observation gap is carried into the forecast. Weather model errors are
# mostly about timing (a front or rain band early or late), which rarely persists past a few hours.
FORECAST_BIAS_FADE_H = 3.0
CONF_TIER2_ZONES = "tier2_zones"
CONF_TIER3_ZONES = "tier3_zones"
CONF_CONSTANT_ZONES = "constant_zones"
CONF_GRID_POWER = "grid_power"
CONF_GRID_IMPORT_NEGATIVE = "grid_import_negative"
CONF_AC_PHASE_POWER = "ac_phase_power"
CONF_AUTOMATIONS_TO_DISABLE = "automations_to_disable"

# --- tuning options ---
CONF_MIN_RUN = "min_run_minutes"
CONF_MIN_OFF = "min_off_minutes"
CONF_COOL_DEPTH = "cool_depth"
CONF_HEAT_DEPTH = "heat_depth"
CONF_PRECOOL_DEPTH = "precool_depth"
CONF_HORIZON = "horizon_hours"
CONF_START_LAG = "start_lag_minutes"
CONF_TOLERANCE = "tolerance_c"
CONF_OVERRIDE_MINUTES = "override_minutes"
CONF_MIN_OPEN_ZONES = "min_open_zones"
CONF_PRECOOL_MIN_SURPLUS = "precool_min_surplus_w"
CONF_PRECOOL_MIN_OUTDOOR = "precool_min_outdoor_c"
CONF_PRECOOL_START_HOUR = "precool_start_hour"
CONF_IMPORT_TARIFF = "import_tariff"
CONF_FEED_IN_TARIFF = "feed_in_tariff"
CONF_ALLOW_HEATING = "allow_heating"
CONF_ALLOW_COOLING = "allow_cooling"
CONF_NOTIFY_SERVICE = "notify_service"
CONF_NOTIFY_TIME = "notify_time"
CONF_RECOVERY_MAX = "recovery_max_c"
CONF_RECOVERY_MINUTES = "recovery_minutes"

DEFAULTS = {
    CONF_GRID_IMPORT_NEGATIVE: True,
    CONF_MIN_RUN: 30,
    CONF_MIN_OFF: 20,
    CONF_COOL_DEPTH: 1.5,
    CONF_HEAT_DEPTH: 1.5,
    CONF_PRECOOL_DEPTH: 3.0,
    CONF_HORIZON: 6,
    CONF_START_LAG: 10,
    CONF_TOLERANCE: 0.3,
    CONF_OVERRIDE_MINUTES: 120,
    CONF_MIN_OPEN_ZONES: 3,
    CONF_PRECOOL_MIN_SURPLUS: 2500,
    CONF_PRECOOL_MIN_OUTDOOR: 28.0,
    CONF_PRECOOL_START_HOUR: 13,
    CONF_IMPORT_TARIFF: 0.0,
    CONF_FEED_IN_TARIFF: 0.0,
    CONF_ALLOW_HEATING: True,
    CONF_ALLOW_COOLING: True,
    CONF_NOTIFY_TIME: "17:00:00",
    CONF_RECOVERY_MAX: 0.7,
    CONF_RECOVERY_MINUTES: 45,
}

# --- live-adjustable bands (number entities) ---
BAND_DEFAULTS = {
    "tier1_min": 21.0,
    "tier1_max": 25.0,
    "tier2_min": 19.0,
    "tier2_max": 25.0,
    "tier3_min": 17.0,
    "tier3_max": 27.0,
    "day_min": 18.0,
    "day_max": 26.0,
}
WINDOW_DEFAULTS = {"window_start": "17:30", "window_end": "07:00"}

CONTROL_INTERVAL_MIN = 5
SAMPLE_INTERVAL_SEC = 60
DEFAULT_AC_WATTS = 4500.0
