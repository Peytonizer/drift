"""Fit a zone's thermal model from recorder history (pure Python).

Online learning alone needs two or three nights before predictions settle, and a single
evening can mislead it (a house still giving off a hot day's heat looks like a room that
never cools). Replaying the last week of history gives the model those nights up front.

Steps are 15 minutes rather than the 5 used online: the sensors report in 0.1 °C steps,
so a 5-minute change is mostly rounding, while 15 minutes carries a usable rate.
The same rules as online learning decide which intervals count:
* passive: the AC was off for the whole interval and for 20 minutes before it;
* active: the AC was in one mode for the whole interval, had been running 10+ minutes,
  and the zone was open and still short of its setpoint.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta

from .model import ZoneModel, solar_factor

STEP = timedelta(minutes=15)
SETTLE_OFF = timedelta(minutes=20)
SETTLE_ON = timedelta(minutes=10)


@dataclass
class Series:
    """A step function from (time, value) samples: the value holds until the next sample."""

    times: list[datetime]
    values: list

    @classmethod
    def from_points(cls, points: list[tuple[datetime, object]]) -> "Series":
        pts = sorted((p for p in points if p[1] is not None), key=lambda p: p[0])
        return cls([t for t, _ in pts], [v for _, v in pts])

    def at(self, when: datetime):
        i = bisect_right(self.times, when) - 1
        return self.values[i] if i >= 0 else None

    def last_change_before(self, when: datetime) -> datetime | None:
        """Time the value at `when` took effect."""
        i = bisect_right(self.times, when) - 1
        if i < 0:
            return None
        v = self.values[i]
        while i > 0 and self.values[i - 1] == v:
            i -= 1
        return self.times[i]


def fit_from_history(
    model: ZoneModel,
    temps: list[tuple[datetime, float]],
    ac_modes: list[tuple[datetime, str]],
    outdoor: list[tuple[datetime, float, float | None]],
    zone_state: list[tuple[datetime, tuple[bool, float | None]]] | None,
    lat: float,
    lon: float,
    start: datetime,
    end: datetime,
) -> dict:
    """Replay history into `model`. Returns counts of the samples used."""
    t_series = Series.from_points(temps)
    m_series = Series.from_points(ac_modes)
    o_series = Series.from_points([(t, (v, c)) for t, v, c in outdoor])
    z_series = Series.from_points(zone_state) if zone_state else None
    passive = active = 0
    t0 = start
    while t0 + STEP <= end:
        t1 = t0 + STEP
        v0, v1 = t_series.at(t0), t_series.at(t1)
        m0, m1 = m_series.at(t0), m_series.at(t1)
        o = o_series.at(t0 + STEP / 2)
        if v0 is None or v1 is None or m0 is None or m1 is None or o is None:
            t0 = t1
            continue
        observed = (v1 - v0) / (STEP.total_seconds() / 3600)
        t_out, cloud = o
        sol = solar_factor(t0 + STEP / 2, lat, lon, cloud)
        since = m_series.last_change_before(t1)
        if m0 == m1 == "off":
            if since is not None and since <= t0 - SETTLE_OFF:
                model.update_passive(v0, t_out, sol, observed)
                passive += 1
        elif m0 == m1 and m0 in ("cool", "heat") and since is not None and since <= t0 - SETTLE_ON:
            zs = z_series.at(t0) if z_series else (True, None)
            if zs is not None:
                is_open, setpoint = zs
                short = setpoint is None or (v0 > setpoint + 0.3 if m0 == "cool" else v0 < setpoint - 0.3)
                if is_open and short:
                    model.update_active(m0, observed, model.passive_rate(v0, t_out, sol))
                    active += 1
        t0 = t1
    return {"passive": passive, "active": active}
