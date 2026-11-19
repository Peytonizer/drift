"""Nightly review + tuning suggestions (pure Python).

During the overnight window a sample is recorded every minute. At the end of the
window the night is summarised (comfort, cycling, energy, forecast accuracy) and
a set of plain-English tuning suggestions is generated from the last few nights.
Suggestions are advice only — nothing is changed automatically.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from statistics import mean


@dataclass
class ZoneSample:
    temp: float
    lo: float
    hi: float


@dataclass
class NightRecorder:
    started: str | None = None
    minutes: int = 0
    zone_minutes: dict = field(default_factory=dict)  # zone -> {"in": n, "total": n, "min": x, "max": x, "over_dm": x, "under_dm": x}
    ac_minutes: int = 0
    starts: int = 0
    ac_kwh: float = 0.0
    solar_kwh: float = 0.0
    grid_kwh: float = 0.0
    pred_errors: list = field(default_factory=list)  # |predicted 60 min ago - actual| for tier-1
    first_hour_out: int = 0  # minutes tier-1 out of band in first hour (ready-by check)
    tog: str | None = None  # sleepwear in use that night

    def sample(self, zones: dict[str, tuple[int, ZoneSample]], ac_on: bool, minute_of_window: int) -> None:
        self.minutes += 1
        if ac_on:
            self.ac_minutes += 1
        for name, (tier, s) in zones.items():
            zm = self.zone_minutes.setdefault(
                name, {"tier": tier, "in": 0, "total": 0, "min": s.temp, "max": s.temp, "over_dm": 0.0, "under_dm": 0.0}
            )
            zm["total"] += 1
            zm["min"] = min(zm["min"], s.temp)
            zm["max"] = max(zm["max"], s.temp)
            if s.lo <= s.temp <= s.hi:
                zm["in"] += 1
            elif s.temp > s.hi:
                zm["over_dm"] += s.temp - s.hi
            else:
                zm["under_dm"] += s.lo - s.temp
            if tier == 1 and minute_of_window < 60 and not (s.lo <= s.temp <= s.hi):
                self.first_hour_out += 1

    def to_dict(self) -> dict:
        return dict(vars(self))

    @classmethod
    def from_dict(cls, d: dict | None) -> "NightRecorder":
        x = cls()
        for k, v in (d or {}).items():
            if hasattr(x, k):
                setattr(x, k, v)
        return x


def summarise(rec: NightRecorder, date_label: str, import_tariff: float, feed_in_tariff: float) -> dict:
    zones = {}
    for name, zm in rec.zone_minutes.items():
        total = max(zm["total"], 1)
        zones[name] = {
            "tier": zm["tier"],
            "in_band_pct": round(100 * zm["in"] / total, 1),
            "min": round(zm["min"], 1),
            "max": round(zm["max"], 1),
            "degree_hours_over": round(zm["over_dm"] / 60, 2),
            "degree_hours_under": round(zm["under_dm"] / 60, 2),
        }
    tier1 = [z for z in zones.values() if z["tier"] == 1]
    cost = None
    if import_tariff:
        cost = round(rec.grid_kwh * import_tariff + rec.solar_kwh * feed_in_tariff, 2)
    return {
        "night": date_label,
        "tog": rec.tog,
        "minutes": rec.minutes,
        "tier1_in_band_pct": tier1[0]["in_band_pct"] if tier1 else None,
        "tier1_ready_minutes_out": rec.first_hour_out,
        "zones": zones,
        "compressor_starts": rec.starts,
        "runtime_h": round(rec.ac_minutes / 60, 2),
        "ac_kwh": round(rec.ac_kwh, 2),
        "solar_kwh": round(rec.solar_kwh, 2),
        "grid_kwh": round(rec.grid_kwh, 2),
        "est_cost": cost,
        "forecast_mae_c": round(mean(rec.pred_errors), 2) if rec.pred_errors else None,
    }


def suggestions(nights: list[dict], settings: dict, precool_enabled: bool) -> list[str]:
    """Rules of thumb over the last up-to-7 nights."""
    recent = nights[-7:]
    if not recent:
        return []
    out: list[str] = []
    n = len(recent)
    starts = mean(x["compressor_starts"] for x in recent)
    t1 = [x["tier1_in_band_pct"] for x in recent if x.get("tier1_in_band_pct") is not None]
    ready_late = sum(1 for x in recent if x.get("tier1_ready_minutes_out", 0) > 10)
    mae = [x["forecast_mae_c"] for x in recent if x.get("forecast_mae_c") is not None]
    grid_share = None
    tot = sum(x["ac_kwh"] for x in recent)
    if tot > 0.5:
        grid_share = sum(x["grid_kwh"] for x in recent) / tot

    if starts > 4:
        out.append(
            f"Averaging {starts:.1f} compressor starts a night. Consider raising 'cool depth' "
            f"(now {settings.get('cool_depth')}°C) or 'minimum off time' (now {settings.get('min_off_minutes')} min) "
            "so each run lasts longer."
        )
    if t1 and mean(t1) < 95:
        out.append(
            f"Tier-1 room was in band only {mean(t1):.0f}% of the night on average. "
            f"Increase 'start lag' (now {settings.get('start_lag_minutes')} min) so the AC starts earlier."
        )
    if ready_late >= max(2, n // 3):
        out.append(
            f"On {ready_late} of {n} nights the tier-1 room wasn't in band for the start of the window. "
            "The pre-start lead looks too short — increase 'start lag' by ~10 minutes."
        )
    if t1 and min(t1) >= 99.5 and starts <= 1:
        zones_max = [x["zones"] for x in recent]
        headroom = []
        for zs in zones_max:
            for z in zs.values():
                if z["tier"] == 1:
                    headroom.append(z["max"])
        if headroom and max(headroom) < 24.0 and tot > 1:
            out.append(
                "Tier-1 room stayed comfortably inside its band every night with room to spare. "
                "You could reduce 'cool depth' by 0.5°C to save energy."
            )
    if mae and mean(mae) > 0.6:
        out.append(
            f"Forecast accuracy is {mean(mae):.1f}°C off on average 1 h ahead — the room model is still learning "
            "(or a door/window is changing behaviour). Re-check after a few more nights."
        )
    if grid_share is not None and grid_share > 0.7 and not precool_enabled:
        out.append(
            f"{grid_share * 100:.0f}% of AC energy came from the grid. Enabling solar pre-cool could shift some of that onto your panels."
        )
    for x in recent[-1:]:
        for name, z in x["zones"].items():
            if z["tier"] == 2 and z["in_band_pct"] < 80:
                out.append(f"{name} (tier 2) was only in band {z['in_band_pct']}% of last night — consider widening its band or raising its priority.")
    return out
