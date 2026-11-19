"""Forecast-aware planner (pure Python, no HA imports).

Every control cycle the planner:
  1. projects each zone's temperature forward with the AC OFF, using the
     learned thermal model and the hourly outdoor forecast;
  2. works out, for every future moment the zone would leave its comfort band,
     the latest time the AC could start and still prevent it (lead time);
  3. starts only when that latest start time has arrived (so the room is given
     every chance to sort itself out naturally), runs in decent blocks
     (min run / min off) to avoid short-cycling, and stops once the zones it
     was running for reach target or the forecast says they no longer need it;
  4. picks zone setpoints, which zones are open and the MyZone by tier priority.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from .model import ZoneModel, solar_factor

BandFn = Callable[[int, datetime], tuple[float, float]]


@dataclass
class ZoneInput:
    zone_id: str
    name: str
    tier: int  # 1 = highest priority
    temp: float | None
    model: ZoneModel
    constant: bool = False  # the AC's pressure-relief zone: never switched off by Drift


@dataclass
class ForecastPoint:
    when: datetime
    temp: float
    cloud: float | None = None


@dataclass
class Settings:
    horizon_h: float = 6
    step_min: int = 5
    cool_depth: float = 1.5
    heat_depth: float = 1.5
    precool_depth: float = 3.0
    min_run_min: int = 30
    min_off_min: int = 20
    start_lag_min: int = 10
    tolerance_c: float = 0.3
    tolerance_minutes: int = 20
    recovery_max_c: float = 0.7
    recovery_minutes: int = 45
    hard_margin_c: float = 1.0
    min_open_zones: int = 3
    precool_enabled: bool = True
    precool_start_hour: int = 13
    precool_min_outdoor: float = 28.0
    precool_min_surplus_w: float = 2500
    allow_cooling: bool = True
    allow_heating: bool = True


@dataclass
class PlanInput:
    now: datetime  # timezone-aware, local
    zones: list[ZoneInput]
    forecast: list[ForecastPoint]
    outdoor_now: float | None
    cloud_now: float | None
    lat: float
    lon: float
    band_fn: BandFn
    window_start: datetime  # next (or current) overnight window start
    ac_mode: str  # off | cool | heat
    ac_since: datetime | None  # when current ac_mode began
    last_stop: datetime | None
    running_precool: bool = False
    solar_surplus_w: float | None = None  # export available if AC were off
    settings: Settings = field(default_factory=Settings)


@dataclass
class ZoneCommand:
    on: bool
    setpoint: float


@dataclass
class Need:
    zone_id: str
    tier: int
    mode: str  # cool | heat
    latest_start: datetime
    violation_at: datetime
    peak_excess: float
    degree_minutes: float


@dataclass
class Recovery:
    """A zone already outside its band that the forecast brings back on its own."""

    zone_id: str
    mode: str  # the mode it would otherwise need
    excess_now: float
    back_in_band: datetime


@dataclass
class Decision:
    action: str  # off | cool | heat
    reason: str
    precool: bool = False
    driver_zone: str | None = None
    myzone: str | None = None
    zone_commands: dict[str, ZoneCommand] = field(default_factory=dict)
    predictions: dict[str, list[tuple[datetime, float]]] = field(default_factory=dict)
    needs: list[Need] = field(default_factory=list)
    recoveries: list[Recovery] = field(default_factory=list)
    next_start: datetime | None = None


# --------------------------------------------------------------------------- helpers
def outdoor_at(inp: PlanInput, when: datetime) -> tuple[float, float | None]:
    """Linear interpolation of outdoor temp/cloud, anchored on the current reading."""
    pts = sorted(inp.forecast, key=lambda p: p.when)
    if inp.outdoor_now is not None:
        pts = [ForecastPoint(inp.now, inp.outdoor_now, inp.cloud_now)] + [p for p in pts if p.when > inp.now]
    if not pts:
        return (inp.outdoor_now if inp.outdoor_now is not None else 20.0), inp.cloud_now
    if when <= pts[0].when:
        return pts[0].temp, pts[0].cloud
    for p0, p1 in zip(pts, pts[1:]):
        if p0.when <= when <= p1.when:
            span = (p1.when - p0.when).total_seconds() or 1
            f = (when - p0.when).total_seconds() / span
            cloud = None
            if p0.cloud is not None and p1.cloud is not None:
                cloud = p0.cloud + f * (p1.cloud - p0.cloud)
            return p0.temp + f * (p1.temp - p0.temp), cloud
    return pts[-1].temp, pts[-1].cloud


def project_passive(inp: PlanInput, zone: ZoneInput, start_temp: float | None = None) -> list[tuple[datetime, float]]:
    s = inp.settings
    temp = zone.temp if start_temp is None else start_temp
    if temp is None:
        return []
    out = [(inp.now, temp)]
    dt_h = s.step_min / 60
    steps = int(s.horizon_h * 60 / s.step_min)
    t = inp.now
    for _ in range(steps):
        t_out, cloud = outdoor_at(inp, t)
        sol = solar_factor(t, inp.lat, inp.lon, cloud)
        temp = temp + zone.model.passive_rate(temp, t_out, sol) * dt_h
        t = t + timedelta(minutes=s.step_min)
        out.append((t, temp))
    return out


def _episodes(inp: PlanInput, zone: ZoneInput, traj: list[tuple[datetime, float]], mode: str) -> list[dict]:
    """Split the projection into contiguous stretches outside the band.

    Each episode records its points (time, excess), when it starts, and when the room is
    back in band (None if it is still out at the end of the horizon).
    """
    episodes: list[dict] = []
    cur: dict | None = None
    for when, temp in traj:
        lo, hi = inp.band_fn(zone.tier, when)
        excess = (temp - hi) if mode == "cool" else (lo - temp)
        if excess > 0:
            if cur is None:
                cur = {"start": when, "points": [], "end": None}
                episodes.append(cur)
            cur["points"].append((when, excess))
        elif cur is not None:
            cur["end"] = when
            cur = None
    return episodes


def find_need(
    inp: PlanInput,
    zone: ZoneInput,
    traj: list[tuple[datetime, float]],
    recoveries: list[Recovery] | None = None,
) -> list[Need]:
    """Latest-start analysis for cooling and heating, one need per out-of-band episode.

    Two kinds of excursion are left alone:
    * small, short ones anywhere in the horizon (tolerance), and
    * a room that is already over (or under) its band by a little, when the forecast has
      it back in band soon on its own (natural recovery). Recovery only applies to the
      episode the room is in now: a future breach is still planned against, so the room
      is in band when it should be (e.g. at bedtime).
    The hard limit in plan() overrides both.
    """
    s = inp.settings
    needs: list[Need] = []
    lag = timedelta(minutes=s.start_lag_min)
    for mode in ("cool", "heat"):
        if mode == "cool" and not s.allow_cooling:
            continue
        if mode == "heat" and not s.allow_heating:
            continue
        rate = zone.model.cool_rate if mode == "cool" else zone.model.heat_rate
        for ep in _episodes(inp, zone, traj, mode):
            pts = ep["points"]
            peak = max(e for _, e in pts)
            minutes_over = len(pts) * s.step_min
            deg_min = sum(e for _, e in pts) * s.step_min
            if peak <= s.tolerance_c and (minutes_over <= s.tolerance_minutes or deg_min <= s.tolerance_c * 30):
                continue
            if (
                ep["start"] <= inp.now
                and ep["end"] is not None
                and peak <= s.recovery_max_c
                and ep["end"] - inp.now <= timedelta(minutes=s.recovery_minutes)
            ):
                if recoveries is not None:
                    recoveries.append(Recovery(zone.zone_id, mode, pts[0][1], ep["end"]))
                continue
            latest = min(when - timedelta(hours=e / max(rate, 0.1)) - lag for when, e in pts)
            needs.append(Need(zone.zone_id, zone.tier, mode, latest, ep["start"], peak, deg_min))
    return needs


def _hard_violation(inp: PlanInput, zone: ZoneInput, mode: str) -> bool:
    if zone.temp is None:
        return False
    lo, hi = inp.band_fn(zone.tier, inp.now)
    m = inp.settings.hard_margin_c
    return zone.temp > hi + m if mode == "cool" else zone.temp < lo - m


def _target(inp: PlanInput, zone: ZoneInput, mode: str, precool: bool) -> float:
    s = inp.settings
    lo, hi = inp.band_fn(zone.tier, inp.now)
    # aim for the band that applies over the next couple of hours, so pre-cooling
    # before the overnight window targets the overnight band, not the day band
    ahead = max(inp.now + timedelta(hours=2), inp.window_start) if precool else inp.now + timedelta(hours=2)
    lo2, hi2 = inp.band_fn(zone.tier, ahead)
    lo, hi = max(lo, lo2), min(hi, hi2)
    if lo > hi:
        lo, hi = lo2, hi2
    if mode == "cool":
        depth = s.precool_depth if precool else s.cool_depth
        return round(max(lo + 0.5, hi - depth), 1)
    return round(min(hi - 0.5, lo + s.heat_depth), 1)


def _zone_commands(inp: PlanInput, mode: str, precool: bool, needing: set[str]) -> tuple[dict[str, ZoneCommand], str | None]:
    s = inp.settings
    cmds: dict[str, ZoneCommand] = {}
    zones = [z for z in inp.zones if z.temp is not None]
    for z in zones:
        target = _target(inp, z, mode, precool)
        lo, hi = inp.band_fn(z.tier, inp.now)
        if z.tier == 1 or z.constant:
            # Constant zones are how the AC relieves duct pressure; leaving them on (with a
            # setpoint) lets its controller open them whenever it needs to.
            on = True
        elif z.zone_id in needing:
            on = True
        elif z.tier == 2:
            on = z.temp > target if mode == "cool" else z.temp < target
        else:  # tier 3: piggyback only when drifting towards the edge of its band
            on = z.temp > hi - 1.0 if mode == "cool" else z.temp < lo + 1.0
        cmds[z.zone_id] = ZoneCommand(on, target)
    # protect the ductwork: keep a minimum number of zones open
    open_count = sum(1 for c in cmds.values() if c.on)
    if open_count < s.min_open_zones:
        closed = [z for z in zones if not cmds[z.zone_id].on]
        closed.sort(key=lambda z: (z.tier, -(z.temp or 0) if mode == "cool" else (z.temp or 0)))
        for z in closed[: s.min_open_zones - open_count]:
            cmds[z.zone_id].on = True
    # MyZone: highest-priority zone that still needs conditioning
    driver = None
    candidates = sorted(
        (z for z in zones if cmds[z.zone_id].on),
        key=lambda z: z.tier,
    )
    for z in candidates:
        tgt = cmds[z.zone_id].setpoint
        if (mode == "cool" and z.temp > tgt) or (mode == "heat" and z.temp < tgt):
            driver = z.zone_id
            break
    if driver is None and candidates:
        driver = candidates[0].zone_id
    return cmds, driver


def _pick_mode(needs: list[Need]) -> str | None:
    if not needs:
        return None
    best = sorted(needs, key=lambda n: (n.tier, n.latest_start, -n.peak_excess))[0]
    return best.mode


def _fmt(t: datetime) -> str:
    return t.strftime("%H:%M")


# --------------------------------------------------------------------------- main entry
def plan(inp: PlanInput) -> Decision:
    s = inp.settings
    now = inp.now
    step = timedelta(minutes=s.step_min)
    preds: dict[str, list[tuple[datetime, float]]] = {}
    needs: list[Need] = []
    recoveries: list[Recovery] = []
    zones_by_id = {z.zone_id: z for z in inp.zones}
    for z in inp.zones:
        traj = project_passive(inp, z)
        preds[z.zone_id] = traj
        if traj:
            needs.extend(find_need(inp, z, traj, recoveries))

    due = [n for n in needs if n.latest_start <= now + step]
    hard = [
        (z, m)
        for z in inp.zones
        for m in ("cool", "heat")
        if ((m == "cool" and s.allow_cooling) or (m == "heat" and s.allow_heating)) and _hard_violation(inp, z, m)
    ]

    def build(action: str, reason: str, precool: bool = False) -> Decision:
        d = Decision(action=action, reason=reason, precool=precool, predictions=preds, needs=needs, recoveries=recoveries)
        if action in ("cool", "heat"):
            needing = {n.zone_id for n in needs if n.mode == action}
            d.zone_commands, d.myzone = _zone_commands(inp, action, precool, needing)
            d.driver_zone = d.myzone
        future = [n.latest_start for n in needs if n.latest_start > now]
        d.next_start = min(future) if future else None
        return d

    runtime = (now - inp.ac_since) if (inp.ac_mode != "off" and inp.ac_since) else timedelta(0)
    off_for = (now - inp.last_stop) if inp.last_stop else timedelta(days=1)
    min_run = timedelta(minutes=s.min_run_min)
    min_off = timedelta(minutes=s.min_off_min)

    # ------------------------------------------------------------ currently running
    if inp.ac_mode in ("cool", "heat"):
        mode = inp.ac_mode
        precool = inp.running_precool
        # zones still above/below their target (the reason we're running)
        unsatisfied = []
        for z in inp.zones:
            if z.temp is None:
                continue
            tgt = _target(inp, z, mode, precool)
            lo, hi = inp.band_fn(z.tier, now)
            if z.tier <= 2:
                if (mode == "cool" and z.temp > tgt + 0.2) or (mode == "heat" and z.temp < tgt - 0.2):
                    unsatisfied.append(z)
            elif (mode == "cool" and z.temp > hi) or (mode == "heat" and z.temp < lo):
                unsatisfied.append(z)
        future_need = [n for n in needs if n.mode == mode]
        opposite_due = [n for n in due if n.mode != mode and n.tier == 1]

        if runtime < min_run and not opposite_due:
            return build(mode, f"{mode.title()}ing (minimum run until {_fmt(inp.ac_since + min_run)})", precool)
        if opposite_due:
            return build("off", f"Stopping {mode}: {zones_by_id[opposite_due[0].zone_id].name} now needs {opposite_due[0].mode}")
        if precool and inp.solar_surplus_w is not None and inp.solar_surplus_w < 0.5 * s.precool_min_surplus_w and not future_need:
            return build("off", "Pre-cool stopped: solar surplus has dropped")
        if not unsatisfied:
            return build("off", f"{mode.title()}ing complete: zones reached target")
        if not future_need and not hard:
            return build("off", f"Stopping {mode}: forecast says zones stay in band without it")
        names = ", ".join(z.name for z in sorted(unsatisfied, key=lambda z: z.tier)[:3])
        return build(mode, f"{mode.title()}ing until target: {names}", precool)

    # ------------------------------------------------------------ currently off
    hard_modes = [m for _, m in hard]
    if hard:
        z, m = sorted(hard, key=lambda zm: zm[0].tier)[0]
        if off_for >= timedelta(minutes=5):  # hard limit overrides min-off (5 min debounce)
            return build(m, f"{z.name} is {z.temp:.1f}°C, well outside its band — {m}ing now")

    mode = _pick_mode(due)
    if mode:
        n = sorted([x for x in due if x.mode == mode], key=lambda x: (x.tier, x.latest_start))[0]
        zname = zones_by_id[n.zone_id].name
        if off_for < min_off and mode not in hard_modes:
            resume = inp.last_stop + min_off
            d = build("off", f"Waiting for minimum off time (until {_fmt(resume)}) before {mode}ing for {zname}")
            d.next_start = resume
            return d
        return build(
            mode,
            f"{zname} forecast to reach {'above' if mode == 'cool' else 'below'} band at "
            f"{_fmt(n.violation_at)} (by {n.peak_excess:.1f}°C) — starting {mode} now",
        )

    # solar pre-cool: bank coolness on hot afternoons while the panels are producing
    if (
        s.precool_enabled
        and s.allow_cooling
        and inp.solar_surplus_w is not None
        and inp.solar_surplus_w >= s.precool_min_surplus_w
        and s.precool_start_hour <= now.hour
        and now < inp.window_start
        and off_for >= min_off
    ):
        rest_of_day = [p.temp for p in inp.forecast if now <= p.when <= now.replace(hour=23, minute=59)]
        hot = max(rest_of_day + ([inp.outdoor_now] if inp.outdoor_now is not None else []), default=0) >= s.precool_min_outdoor
        evening_need = [n for n in needs if n.mode == "cool" and n.tier <= 2]
        if hot and evening_need:
            zname = zones_by_id[evening_need[0].zone_id].name
            return build(
                "cool",
                f"Solar pre-cool: {inp.solar_surplus_w / 1000:.1f} kW spare and {zname} will need cooling later",
                precool=True,
            )

    recovering = ""
    if recoveries:
        r = sorted(recoveries, key=lambda x: zones_by_id[x.zone_id].tier)[0]
        side = "over" if r.mode == "cool" else "under"
        recovering = (
            f"{zones_by_id[r.zone_id].name} is {r.excess_now:.1f}°C {side} its band but forecast back in band "
            f"by {_fmt(r.back_in_band)} on its own — letting it recover"
        )
    if needs:
        n = sorted(needs, key=lambda x: x.latest_start)[0]
        zname = zones_by_id[n.zone_id].name
        msg = (
            f"Holding off: {zname} projected out of band at {_fmt(n.violation_at)}; "
            f"will start {n.mode} around {_fmt(max(n.latest_start, now))}"
        )
        return build("off", f"{recovering}. {msg}" if recovering else msg)
    if recovering:
        return build("off", recovering)
    return build("off", "All zones forecast to stay in band — letting the house drift naturally")
