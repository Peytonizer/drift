"""Pure-logic tests for the planner, model, energy and review modules."""
from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "drift"

# Load the pure modules without importing Home Assistant (package __init__ needs HA).
pkg = types.ModuleType("sc")
pkg.__path__ = [str(ROOT)]
sys.modules["sc"] = pkg
for name in ("const", "model", "planner", "energy", "review"):
    spec = importlib.util.spec_from_file_location(f"sc.{name}", ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"sc.{name}"] = mod
    spec.loader.exec_module(mod)

from sc.model import ZoneModel, solar_factor  # noqa: E402
from sc.planner import ForecastPoint, PlanInput, Settings, ZoneInput, plan, project_passive  # noqa: E402
from sc.energy import AcPowerLearner, split_solar_grid  # noqa: E402
from sc.review import NightRecorder, ZoneSample, summarise, suggestions  # noqa: E402

AEDT = timezone(timedelta(hours=11))
LAT, LON = -33.87, 151.21
BANDS = {1: (21, 25), 2: (19, 25), 3: (17, 27)}
DAY = (18, 26)


def band_fn_factory(start="17:30", end="07:00"):
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))

    def band(tier, when):
        mins = when.hour * 60 + when.minute
        s, e = sh * 60 + sm, eh * 60 + em
        in_win = mins >= s or mins < e
        return BANDS[tier] if in_win else DAY

    return band


def zones(nursery, master, others=23.5):
    return [
        ZoneInput("nursery", "Nursery", 1, nursery, ZoneModel()),
        ZoneInput("master", "Master", 2, master, ZoneModel()),
        ZoneInput("lounge", "Lounge", 3, others, ZoneModel()),
        ZoneInput("office", "Office", 3, others, ZoneModel()),
        ZoneInput("living", "Living", 3, others, ZoneModel()),
    ]


def mk(now, zs, forecast, outdoor_now, ac_mode="off", ac_since=None, last_stop=None, cloud=90, surplus=None, **kw):
    ws = now.replace(hour=17, minute=30, second=0, microsecond=0)
    if now >= ws:
        pass
    elif now.hour < 7:
        ws = ws - timedelta(days=1)
    return PlanInput(
        now=now, zones=zs, forecast=forecast, outdoor_now=outdoor_now, cloud_now=cloud, lat=LAT, lon=LON,
        band_fn=band_fn_factory(), window_start=ws, ac_mode=ac_mode, ac_since=ac_since, last_stop=last_stop,
        solar_surplus_w=surplus, settings=Settings(**kw),
    )


# Tonight's real met.no hourly forecast for a southern-Australian city (a mild spring evening), UTC -> AEDT
TONIGHT = [
    ("2026-11-19T08:00:00+00:00", 25.0, 100), ("2026-11-19T09:00:00+00:00", 22.3, 99),
    ("2026-11-19T10:00:00+00:00", 22.0, 94), ("2026-11-19T11:00:00+00:00", 21.5, 98),
    ("2026-11-19T12:00:00+00:00", 20.8, 99), ("2026-11-19T13:00:00+00:00", 20.2, 100),
    ("2026-11-19T14:00:00+00:00", 20.4, 99), ("2026-11-19T15:00:00+00:00", 19.9, 99),
    ("2026-11-19T16:00:00+00:00", 20.2, 100), ("2026-11-19T17:00:00+00:00", 19.4, 100),
]


def fc(rows):
    return [ForecastPoint(datetime.fromisoformat(t).astimezone(AEDT), temp, cloud) for t, temp, cloud in rows]


def hot_forecast(day: datetime, temps: dict[int, float]):
    return [ForecastPoint(day.replace(hour=h, minute=0, second=0, microsecond=0), t, 10) for h, t in temps.items()]


# ------------------------------------------------------------------ tests
def test_tonight_holds_off_because_forecast_cools():
    now = datetime(2026, 11, 19, 18, 40, tzinfo=AEDT)
    d = plan(mk(now, zones(24.5, 24.4), fc(TONIGHT), 25.0))
    assert d.action == "off", d.reason
    traj = d.predictions["nursery"]
    assert traj[-1][1] < traj[0][1], "room should be projected to cool overnight"


def test_old_threshold_case_room_at_25_1_tonight_still_waits_if_falling():
    now = datetime(2026, 11, 19, 18, 40, tzinfo=AEDT)
    d = plan(mk(now, zones(25.1, 24.4), fc(TONIGHT), 25.0))
    # 0.1 over for a short time while the night cools -> tolerated
    assert d.action == "off", d.reason


def test_ready_by_window_start_precools_before_1730():
    day = datetime(2026, 1, 15, 0, 0, tzinfo=AEDT)
    f = hot_forecast(day, {14: 34, 15: 35, 16: 35, 17: 34, 18: 33, 19: 31, 20: 29, 21: 27, 22: 25, 23: 24})
    started = None
    t = day.replace(hour=16, minute=0)
    temp = 24.6
    while t < day.replace(hour=17, minute=30):
        d = plan(mk(t, zones(temp, 24.0), f, 34.5, cloud=10))
        if d.action == "cool":
            started = t
            break
        temp += 0.08  # warming ~1°C/h
        t += timedelta(minutes=5)
    assert started is not None, "should start before the window so she's in band at 17:30"
    assert started < day.replace(hour=17, minute=30)
    assert started >= day.replace(hour=16, minute=30), f"started too early: {started}"


def test_min_run_then_stop_at_target():
    now = datetime(2026, 1, 15, 21, 0, tzinfo=AEDT)
    f = hot_forecast(now, {21: 28, 22: 26, 23: 24})
    d = plan(mk(now, zones(24.0, 23.0), f, 28, ac_mode="cool", ac_since=now - timedelta(minutes=10)))
    assert d.action == "cool" and "minimum run" in d.reason
    d = plan(mk(now, zones(23.4, 23.0), f, 28, ac_mode="cool", ac_since=now - timedelta(minutes=40)))
    assert d.action == "off", d.reason


def test_min_off_respected():
    now = datetime(2026, 1, 15, 22, 0, tzinfo=AEDT)
    f = hot_forecast(now, {22: 32, 23: 31})
    d = plan(mk(now, zones(25.2, 23.0), f, 32, last_stop=now - timedelta(minutes=5)))
    assert d.action == "off" and "minimum off" in d.reason


def test_hard_limit_overrides_min_off():
    now = datetime(2026, 1, 15, 22, 0, tzinfo=AEDT)
    f = hot_forecast(now, {22: 32, 23: 31})
    d = plan(mk(now, zones(26.5, 23.0), f, 32, last_stop=now - timedelta(minutes=6)))
    assert d.action == "cool"


def test_zone_priorities_when_cooling():
    now = datetime(2026, 1, 15, 22, 0, tzinfo=AEDT)
    f = hot_forecast(now, {22: 32, 23: 31, 0: 30})
    zs = zones(25.6, 24.8, others=24.0)
    d = plan(mk(now, zs, f, 32))
    assert d.action == "cool"
    assert d.myzone == "nursery"
    c = d.zone_commands
    assert c["nursery"].on and c["nursery"].setpoint == 23.5
    assert c["master"].on  # above its target 23.5
    # tier-3 at 24.0 is well inside 17–27: only opened to satisfy min-open-zones (3)
    assert sum(1 for x in c.values() if x.on) == 3


def test_winter_heating_night():
    now = datetime(2026, 7, 10, 19, 0, tzinfo=AEDT)
    f = [ForecastPoint(now + timedelta(hours=h), 5 - h * 0.6, 50) for h in range(8)]
    d = plan(mk(now, zones(21.1, 20.0, others=18), f, 5))
    assert d.action == "heat", d.reason
    assert d.zone_commands["nursery"].setpoint == 22.5


def test_solar_precool_on_hot_afternoon():
    day = datetime(2026, 1, 15, 0, 0, tzinfo=AEDT)
    f = hot_forecast(day, {14: 34, 15: 36, 16: 36, 17: 35, 18: 34, 19: 32, 20: 30, 21: 28, 22: 26})
    now = day.replace(hour=14, minute=0)
    d = plan(mk(now, zones(23.5, 23.0), f, 34, surplus=4000, cloud=0))
    assert d.action == "cool" and d.precool, d.reason
    assert d.zone_commands["nursery"].setpoint == 22.0  # 25 - 3.0, overnight band applies by 17:30
    d2 = plan(mk(now, zones(23.5, 23.0), f, 34, surplus=1000, cloud=0))
    assert d2.action == "off"


def test_fewer_starts_than_threshold_automation():
    """Replay a hot afternoon/evening with a simple room simulator: planner vs old >25/<24 automation."""
    day = datetime(2026, 1, 15, 0, 0, tzinfo=AEDT)
    temps = {12: 30, 13: 32, 14: 33, 15: 34, 16: 34, 17: 33, 18: 31, 19: 29, 20: 27, 21: 25, 22: 23, 23: 22}
    f = hot_forecast(day, temps)
    truth = ZoneModel(a=0.05, b=0.5, cool_rate=3.5)

    def run(policy):
        t, temp, mode, since, last_stop, starts, out_min = day.replace(hour=12), 23.5, "off", None, None, 0, 0
        while t < day.replace(hour=23, minute=55):
            if policy == "old":
                new = "cool" if (mode == "off" and temp > 25) else ("off" if (mode == "cool" and temp < 24) else mode)
            else:
                zs = [ZoneInput("nursery", "Nursery", 1, temp, ZoneModel())]
                d = plan(mk(t, zs, f, None, ac_mode=mode, ac_since=since, last_stop=last_stop, cloud=10, min_open_zones=1))
                new = d.action
            if new != mode:
                if new != "off":
                    starts += 1
                    since = t
                else:
                    last_stop = t
                mode = new
            from sc.planner import outdoor_at
            pi = mk(t, [], f, None)
            tout, cl = outdoor_at(pi, t)
            rate = truth.passive_rate(temp, tout, solar_factor(t, LAT, LON, 10))
            if mode == "cool":
                rate -= truth.cool_rate
            temp += rate * 5 / 60
            lo, hi = band_fn_factory()(1, t)
            if not lo <= temp <= hi:
                out_min += 5
            t += timedelta(minutes=5)
        return starts, out_min

    old_starts, old_out = run("old")
    new_starts, new_out = run("planner")
    print(f"old: {old_starts} starts, {old_out} min out of band | planner: {new_starts} starts, {new_out} min out")
    assert new_starts < old_starts
    assert new_out <= old_out + 15


def test_model_learns_passive_parameters():
    true = ZoneModel(a=0.08, b=1.0, c=0.1)
    m = ZoneModel()
    import random
    random.seed(1)
    for i in range(1500):
        tin = 20 + random.random() * 6
        tout = 10 + random.random() * 25
        sol = random.random()
        m.update_passive(tin, tout, sol, true.passive_rate(tin, tout, sol) + random.gauss(0, 0.05))
    assert abs(m.a - 0.08) < 0.01 and abs(m.b - 1.0) < 0.1


def test_ac_power_learner_from_phase_steps():
    l = AcPowerLearner()
    t0 = datetime(2026, 11, 19, 15, 14, tzinfo=AEDT)
    # observed today: phase B ~+2000 W before, ~-3400 W after start
    for m in range(-8, 12):
        w = 2000 if m < 1 else -3400
        l.add_sample(t0 + timedelta(minutes=m), w)
    l.mark("start", t0)
    for m in range(12, 16):
        l.add_sample(t0 + timedelta(minutes=m), -3400)
    # needs history around the mark: re-feed
    assert l.samples_learned == 1 and l.ac_watts == 5400


def test_split_solar_grid():
    assert split_solar_grid(4500, -3500) == (1000, 3500)
    assert split_solar_grid(4500, 1000) == (4500, 0)
    assert split_solar_grid(4500, None) == (0, 4500)


def test_review_and_suggestions():
    nights = []
    for i in range(5):
        r = NightRecorder(starts=6)
        for m in range(800):
            r.sample({"Nursery": (1, ZoneSample(25.4 if m < 30 else 23, 21, 25)), "Master": (2, ZoneSample(24, 19, 25))}, m % 3 == 0, m)
        r.ac_kwh, r.grid_kwh, r.solar_kwh = 5, 4.5, 0.5
        nights.append(summarise(r, f"n{i}", 0.3, 0.05))
    s = suggestions(nights, {"cool_depth": 1.5, "min_off_minutes": 20, "start_lag_minutes": 10}, precool_enabled=False)
    text = " ".join(s)
    assert "compressor starts" in text and "pre-start lead" in text and "solar pre-cool" in text
    assert nights[0]["est_cost"] == round(4.5 * 0.3 + 0.5 * 0.05, 2)

