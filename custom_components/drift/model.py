"""Per-zone thermal model with online learning (pure Python, no HA imports).

Passive drift (AC off):   dT/dt = a * (T_out - T_in) + b * solar + c      [°C/h]
Active (zone conditioned): dT/dt = passive - cool_rate   (cooling)
                           dT/dt = passive + heat_rate   (heating)

a, b, c are fitted with recursive least squares (with forgetting) from 5-minute
observations while the system is off. cool/heat rates are an exponential moving
average of (observed - passive prediction) while the zone is being conditioned.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math

A_BOUNDS = (0.005, 0.3)
B_BOUNDS = (0.0, 3.0)
C_BOUNDS = (-1.0, 1.0)
RATE_BOUNDS = (0.5, 8.0)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def solar_elevation(when: datetime, lat: float, lon: float) -> float:
    """Approximate solar elevation in degrees (NOAA-style, good to ~1°)."""
    t = when.astimezone(timezone.utc)
    doy = t.timetuple().tm_yday
    hour = t.hour + t.minute / 60 + t.second / 3600
    gamma = 2 * math.pi / 365 * (doy - 1 + (hour - 12) / 24)
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    true_solar_min = hour * 60 + eqtime + 4 * lon
    ha = math.radians(true_solar_min / 4 - 180)
    latr = math.radians(lat)
    cos_zen = math.sin(latr) * math.sin(decl) + math.cos(latr) * math.cos(decl) * math.cos(ha)
    cos_zen = _clamp(cos_zen, -1.0, 1.0)
    return 90 - math.degrees(math.acos(cos_zen))


def solar_factor(when: datetime, lat: float, lon: float, cloud_pct: float | None) -> float:
    """0..1 proxy for solar heat gain."""
    elev = solar_elevation(when, lat, lon)
    if elev <= 0:
        return 0.0
    cloud = 0.0 if cloud_pct is None else _clamp(cloud_pct, 0, 100) / 100
    return math.sin(math.radians(elev)) * (1 - 0.7 * cloud)


@dataclass
class ZoneModel:
    a: float = 0.05
    b: float = 0.6
    c: float = 0.0
    cool_rate: float = 3.0
    heat_rate: float = 2.5
    P: list[list[float]] = field(default_factory=lambda: [[1.0, 0, 0], [0, 1.0, 0], [0, 0, 1.0]])
    n_passive: int = 0
    n_active_cool: int = 0
    n_active_heat: int = 0
    passive_abs_err: float = 0.0  # EMA of |residual| in °C/h

    def passive_rate(self, t_in: float, t_out: float, solar: float) -> float:
        return self.a * (t_out - t_in) + self.b * solar + self.c

    def update_passive(
        self, t_in: float, t_out: float, solar: float, observed_rate: float, lam: float = 0.998
    ) -> None:
        """One RLS step with forgetting factor lam."""
        if abs(observed_rate) > 6:  # implausible (door open, sensor glitch)
            return
        x = [t_out - t_in, solar, 1.0]
        theta = [self.a, self.b, self.c]
        P = self.P
        Px = [sum(P[i][j] * x[j] for j in range(3)) for i in range(3)]
        denom = lam + sum(x[i] * Px[i] for i in range(3))
        if denom <= 1e-9:
            return
        k = [Px[i] / denom for i in range(3)]
        err = observed_rate - sum(theta[i] * x[i] for i in range(3))
        theta = [theta[i] + k[i] * err for i in range(3)]
        xP = [sum(x[i] * P[i][j] for i in range(3)) for j in range(3)]
        newP = [[(P[i][j] - k[i] * xP[j]) / lam for j in range(3)] for i in range(3)]
        # keep covariance bounded so it can't blow up during long idle stretches
        for i in range(3):
            newP[i][i] = _clamp(newP[i][i], 1e-4, 50.0)
        self.P = newP
        self.a = _clamp(theta[0], *A_BOUNDS)
        self.b = _clamp(theta[1], *B_BOUNDS)
        self.c = _clamp(theta[2], *C_BOUNDS)
        self.n_passive += 1
        self.passive_abs_err = 0.95 * self.passive_abs_err + 0.05 * abs(err)

    def update_active(self, mode: str, observed_rate: float, passive_rate: float, alpha: float = 0.08) -> None:
        if mode == "cool":
            sample = passive_rate - observed_rate
            if 0.2 < sample < 12:
                self.cool_rate = _clamp((1 - alpha) * self.cool_rate + alpha * sample, *RATE_BOUNDS)
                self.n_active_cool += 1
        elif mode == "heat":
            sample = observed_rate - passive_rate
            if 0.2 < sample < 12:
                self.heat_rate = _clamp((1 - alpha) * self.heat_rate + alpha * sample, *RATE_BOUNDS)
                self.n_active_heat += 1

    def to_dict(self) -> dict:
        return {
            "a": self.a,
            "b": self.b,
            "c": self.c,
            "cool_rate": self.cool_rate,
            "heat_rate": self.heat_rate,
            "P": self.P,
            "n_passive": self.n_passive,
            "n_active_cool": self.n_active_cool,
            "n_active_heat": self.n_active_heat,
            "passive_abs_err": self.passive_abs_err,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "ZoneModel":
        m = cls()
        if not d:
            return m
        for key in m.to_dict():
            if key in d:
                setattr(m, key, d[key])
        return m
