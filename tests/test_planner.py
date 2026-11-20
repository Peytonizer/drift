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
for name in ("const", "model", "planner", "energy", "review", "tog", "bootstrap", "rules"):
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


ZONE_TIERS = {"nursery": 1, "master": 2}


def band_fn_factory(start="17:30", end="07:00"):
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))

    def band(zone_id, when):
        tier = ZONE_TIERS.get(zone_id, 3)
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
            lo, hi = band_fn_factory()("nursery", t)
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


from sc.tog import DEFAULT_CHART, band_for, chart_from_options, recommend  # noqa: E402


def test_tog_bands_use_the_chart_range_as_is():
    assert band_for("1.0", DEFAULT_CHART) == (20.0, 24.0)
    assert band_for("1.5", DEFAULT_CHART) == (18.0, 22.0)
    assert band_for("0.2", DEFAULT_CHART) == (24.0, 27.0)
    chart = chart_from_options({"tog_1_0_min": 21, "tog_1_0_max": 23})
    assert band_for("1.0", chart) == (21.0, 23.0)


def test_tog_recommendation_follows_natural_overnight_temps():
    assert recommend([23.8, 23.0, 22.4, 21.9, 21.5, 21.2], DEFAULT_CHART)["tog"] == "1.0"
    assert recommend([26.5, 26.0, 25.6, 25.2, 24.9], DEFAULT_CHART)["tog"] == "0.2"
    assert recommend([19.5, 18.4, 17.6, 17.0], DEFAULT_CHART)["tog"] == "2.5"
    # 1.0 (20-24) and 1.5 (18-22) overlap: a room sitting 20-22 all night suits 1.5
    assert recommend([21.8, 21.2, 20.6, 20.2], DEFAULT_CHART)["tog"] == "1.5"
    assert recommend([], DEFAULT_CHART) is None



# ---- natural recovery: already over the limit, forecast says it comes back on its own
TOG10 = {1: (20, 24), 2: (19, 25), 3: (17, 27)}


def band_tog10(zone_id, when):
    tier = ZONE_TIERS.get(zone_id, 3)
    mins = when.hour * 60 + when.minute
    return TOG10[tier] if (mins >= 17 * 60 + 30 or mins < 7 * 60) else DAY


def test_recovers_naturally_when_slightly_over_and_cooling_outside():
    now = datetime(2026, 11, 20, 20, 30, tzinfo=AEDT)
    f = [ForecastPoint(now + timedelta(hours=h), 14 - h * 0.5, 50) for h in range(8)]
    zs = [ZoneInput("nursery", "Nursery", 1, 24.5, ZoneModel(a=0.12))]
    inp = mk(now, zs, f, 15, min_open_zones=1)
    inp.band_fn = band_tog10
    d = plan(inp)
    assert d.action == "off", d.reason
    assert "letting it recover" in d.reason and "0.5°C over" in d.reason
    assert d.recoveries and d.recoveries[0].back_in_band - now <= timedelta(minutes=45)


def test_cools_when_slightly_over_but_warm_outside():
    now = datetime(2026, 1, 15, 20, 30, tzinfo=AEDT)
    f = [ForecastPoint(now + timedelta(hours=h), 31 - h * 0.3, 10) for h in range(8)]
    zs = [ZoneInput("nursery", "Nursery", 1, 24.5, ZoneModel())]
    inp = mk(now, zs, f, 31, min_open_zones=1)
    inp.band_fn = band_tog10
    d = plan(inp)
    assert d.action == "cool", d.reason
    assert d.zone_commands["nursery"].setpoint == 22.5  # chart edge 24 - cool depth 1.5


def test_too_far_over_to_wait_even_if_it_would_recover():
    now = datetime(2026, 11, 20, 20, 30, tzinfo=AEDT)
    f = [ForecastPoint(now + timedelta(hours=h), 10, 50) for h in range(8)]
    zs = [ZoneInput("nursery", "Nursery", 1, 24.9, ZoneModel(a=0.3))]
    inp = mk(now, zs, f, 10, min_open_zones=1)
    inp.band_fn = band_tog10
    d = plan(inp)
    assert d.action == "cool", d.reason  # 0.9 over is beyond the 0.7 recovery allowance


def test_recovery_never_applies_to_a_future_breach():
    """A breach that hasn't happened yet is still planned against (e.g. bedtime)."""
    day = datetime(2026, 1, 15, 0, 0, tzinfo=AEDT)
    f = hot_forecast(day, {16: 30, 17: 29, 18: 26, 19: 22, 20: 19, 21: 17})
    zs = [ZoneInput("nursery", "Nursery", 1, 24.6, ZoneModel(a=0.12))]
    inp = mk(day.replace(hour=17, minute=10), zs, f, 29.5, cloud=10, min_open_zones=1)
    inp.band_fn = band_tog10
    d = plan(inp)
    assert not d.recoveries
    assert d.action == "cool", d.reason



# ---- training the room model from a week of history
from sc.bootstrap import fit_from_history  # noqa: E402


def test_bootstrap_recovers_room_behaviour_from_a_week_of_history():
    """A synthetic week: hot days, cool nights, the AC cooling some evenings.

    The sensor reports in 0.1 °C steps like the real one. The fitted model should find
    the room's coupling to outside and project the overnight fall the history shows.
    """
    import math
    truth = ZoneModel(a=0.07, b=0.5, c=0.08, cool_rate=3.2)
    start = datetime(2026, 11, 12, 0, 0, tzinfo=AEDT)
    end = start + timedelta(days=7)
    temps, modes, outdoor = [], [], []
    t, room, mode = start, 23.0, "off"
    while t < end:
        h = t.hour + t.minute / 60
        tout = 19 + 9 * math.sin((h - 9) / 24 * 2 * math.pi)  # ~10 at 3am, ~28 at 3pm
        sol = solar_factor(t, LAT, LON, 30)
        want = "cool" if (17 <= h < 21 and room > 25) else ("off" if room < 21.5 or not 17 <= h < 21 else mode)
        if want != mode:
            mode = want
            modes.append((t, mode))
        rate = truth.passive_rate(room, tout, sol) - (truth.cool_rate if mode == "cool" else 0)
        room += rate / 60
        temps.append((t, round(room, 1)))
        if t.minute == 0:
            outdoor.append((t, round(tout, 1), 30))
        t += timedelta(minutes=1)
    modes.insert(0, (start, "off"))
    m = ZoneModel()
    counts = fit_from_history(m, temps, modes, outdoor, None, LAT, LON, start, end)
    assert counts["passive"] > 400 and counts["active"] >= 3
    assert abs(m.a - truth.a) < 0.02, m.a
    assert abs(m.cool_rate - truth.cool_rate) < 1.0, m.cool_rate
    # overnight projection from 24.7 at 21:00 on a 15 °C-ish night falls, like the real house
    now = datetime(2026, 11, 20, 21, 0, tzinfo=AEDT)
    f = [ForecastPoint(now + timedelta(hours=k), 16 - k * 0.6, 30) for k in range(12)]
    inp = mk(now, [ZoneInput("nursery", "Nursery", 1, 24.7, m)], f, 16, horizon_h=10)
    traj = project_passive(inp, inp.zones[0])
    assert traj[-1][1] < 23.0



def test_constant_zones_are_never_switched_off():
    now = datetime(2026, 1, 15, 22, 0, tzinfo=AEDT)
    f = hot_forecast(now, {22: 32, 23: 31, 0: 30})
    zs = zones(25.6, 24.8, others=22.0)  # tier 3 well inside its band: normally closed
    zs[2] = ZoneInput("lounge", "Lounge", 3, 22.0, ZoneModel(), constant=True)
    d = plan(mk(t := now, zs, f, 32))
    assert d.action == "cool"
    assert d.zone_commands["lounge"].on
    assert not d.zone_commands["living"].on  # an ordinary tier 3 zone still closes



# ---- daytime comfort rules
from datetime import time as dtime  # noqa: E402
from sc.rules import Rule, SensorPresence, WeeklyBlocks, presence_active, resolve_band, rule_active  # noqa: E402

OFFICE = WeeklyBlocks.from_service(
    {d: [{"from": "08:00:00", "to": "16:30:00"}] for d in ["monday", "tuesday", "wednesday", "thursday", "friday"]}
)


def test_weekly_blocks_cover_the_right_days_and_times():
    mon = datetime(2026, 11, 23, 9, 0, tzinfo=AEDT)  # a Monday
    assert OFFICE.covers(mon)
    assert not OFFICE.covers(mon.replace(hour=7, minute=59))
    assert not OFFICE.covers(mon.replace(hour=16, minute=30))
    assert not OFFICE.covers(mon + timedelta(days=5))  # Saturday
    wb = WeeklyBlocks.from_service({"monday": [{"from": dtime(7, 0), "to": "24:00:00"}]})
    assert wb.covers(mon.replace(hour=23, minute=50))


def test_band_precedence():
    guard, t1, t3 = (16, 28), (20, 24), (17, 27)
    assert resolve_band(1, True, t1, guard, [(21, 25)]) == t1  # tier 1 keeps its night band
    assert resolve_band(3, True, t3, guard, [(21, 25)]) == (21, 25)  # tier 3 takes the rule at night
    assert resolve_band(3, False, t3, guard, []) == guard
    assert resolve_band(3, False, t3, guard, [(21, 25), (22, 26)]) == (22, 25)  # narrowest
    assert resolve_band(3, False, t3, guard, [(21, 22), (24, 26)]) == (21, 22)  # disjoint: first wins


def test_presence_needs_a_real_stay_and_holds_afterwards():
    sat = datetime(2026, 11, 21, 10, 0, tzinfo=AEDT)
    r = Rule("r1", "Weekend", ["climate.ac_lounge"], "presence", presence=["b"], presence_days=["saturday", "sunday"])
    assert not presence_active(r, sat, [SensorPresence(True, sat - timedelta(minutes=3), None)])
    assert presence_active(r, sat, [SensorPresence(True, sat - timedelta(minutes=6), sat)])
    assert presence_active(r, sat, [SensorPresence(False, None, sat - timedelta(minutes=25))])
    assert not presence_active(r, sat, [SensorPresence(False, None, sat - timedelta(minutes=35))])
    tue = sat + timedelta(days=3)
    assert not presence_active(r, tue, [SensorPresence(True, tue - timedelta(minutes=30), tue)])


def test_presence_rule_looks_two_hours_ahead_and_never_runs_at_night():
    sat = datetime(2026, 11, 21, 10, 0, tzinfo=AEDT)
    r = Rule("r1", "Weekend", ["z"], "presence", presence=["b"], presence_days=["saturday"])
    on = {"r1": True}
    assert rule_active(r, sat + timedelta(minutes=90), sat, False, {}, on)
    assert not rule_active(r, sat + timedelta(hours=3), sat, False, {}, on)
    assert not rule_active(r, sat, sat, True, {}, on)
    assert not rule_active(r, sat, sat, False, {}, {"r1": False})


def test_schedule_rule_preconditions_before_it_starts():
    """Office rule 08:00-16:30 at 21-25; empty-room guard 16-28. A warm office at 26.4
    needs cooling to be under 25 by 08:00, so the AC starts shortly before, not at 07:00."""
    mon = datetime(2026, 11, 23, 0, 0, tzinfo=AEDT)
    rule = Rule("o", "Office", ["office"], "schedule", schedule="schedule.office", min=21, max=25)

    def band(zone_id, when):
        in_win = when.hour * 60 + when.minute >= 17 * 60 + 30 or when.hour < 7
        rb = [(rule.min, rule.max)] if zone_id in rule.zones and rule_active(rule, when, when, in_win, {"schedule.office": OFFICE}, {}) else []
        return resolve_band(3, in_win, (17, 27), (16, 28), rb)

    f = hot_forecast(mon, {7: 26, 8: 28, 9: 30, 10: 31})
    def at(h, m):
        now = mon.replace(hour=h, minute=m)
        zs = [ZoneInput("office", "Office", 3, 26.4, ZoneModel(a=0.03, b=0.2))]
        inp = mk(now, zs, f, 26, cloud=50, min_open_zones=1)
        inp.band_fn = band
        return plan(inp)
    assert at(7, 0).action == "off"
    d = at(7, 30)
    assert d.action == "cool", d.reason
    assert d.zone_commands["office"].setpoint == 23.5  # 25 - cool depth, the rule's band ahead


def test_merge_outdoor_history_prefers_observations_and_borrows_cloud():
    from sc.bootstrap import merge_outdoor_history

    t0 = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
    weather = [(t0 + timedelta(hours=h), 25.0, 80.0 + h) for h in range(10)]
    assert merge_outdoor_history(weather, []) == (weather, None)

    obs = [(t0 + timedelta(hours=4, minutes=5), 19.0), (t0 + timedelta(hours=6), None), (t0 + timedelta(hours=7), 18.5)]
    merged, since = merge_outdoor_history(weather, obs)
    assert since == (t0 + timedelta(hours=4, minutes=5)).isoformat()
    assert [p[1] for p in merged] == [25.0] * 5 + [19.0, 18.5]  # model values only before the first observation
    assert merged[5][2] == 84.0 and merged[6][2] == 87.0
