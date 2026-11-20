"""Daytime comfort rules (pure Python).

A rule gives some zones a comfort band while it's active. Two kinds:
* schedule: active when a Home Assistant `schedule` helper's weekly blocks cover the time;
* presence: active while someone has been detected for `dwell` minutes (and for `hold`
  minutes after the last detection), on the listed days only.

Bands resolve in this order for a zone at a moment (first match wins):
1. inside the overnight window, tier 1 and 2 zones: their overnight tier band;
2. any active rule listing the zone: its band (the narrowest if several);
3. inside the overnight window, tier 3: the tier 3 band;
4. otherwise the daytime guard.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
PRESENCE_LOOKAHEAD = timedelta(hours=2)


def _to_time(v) -> time:
    if isinstance(v, time):
        return v
    s = str(v)
    if s.startswith("24:"):
        return time.max
    parts = [int(p) for p in s.split(":")]
    return time(parts[0], parts[1], parts[2] if len(parts) > 2 else 0)


@dataclass
class WeeklyBlocks:
    """A schedule helper's blocks: weekday name -> [(from, to)]."""

    blocks: dict[str, list[tuple[time, time]]] = field(default_factory=dict)

    @classmethod
    def from_service(cls, data: dict) -> "WeeklyBlocks":
        out: dict[str, list[tuple[time, time]]] = {}
        for day in WEEKDAYS:
            out[day] = [(_to_time(b["from"]), _to_time(b["to"])) for b in (data.get(day) or [])]
        return cls(out)

    def covers(self, when: datetime) -> bool:
        day = WEEKDAYS[when.weekday()]
        t = when.timetz().replace(tzinfo=None)
        return any(start <= t < end or (end == time.max and t >= start) for start, end in self.blocks.get(day, []))


@dataclass
class Rule:
    rule_id: str
    name: str
    zones: list[str]
    trigger: str  # schedule | presence
    band_mode: str = "fixed"  # fixed | tier1
    min: float = 21.0
    max: float = 25.0
    schedule: str | None = None
    presence: list[str] = field(default_factory=list)
    presence_days: list[str] = field(default_factory=list)
    dwell_minutes: int = 5
    hold_minutes: int = 30

    @classmethod
    def from_subentry(cls, rule_id: str, title: str, data: dict) -> "Rule":
        return cls(
            rule_id=rule_id,
            name=data.get("name") or title,
            zones=list(data.get("zones") or []),
            trigger=data.get("trigger", "schedule"),
            band_mode=data.get("band", "fixed"),
            min=float(data.get("min", 21.0)),
            max=float(data.get("max", 25.0)),
            schedule=data.get("schedule"),
            presence=list(data.get("presence") or []),
            presence_days=list(data.get("presence_days") or []),
            dwell_minutes=int(data.get("dwell_minutes", 5)),
            hold_minutes=int(data.get("hold_minutes", 30)),
        )


@dataclass
class SensorPresence:
    """One presence sensor's recent history, as the controller sees it."""

    is_on: bool
    on_since: datetime | None  # when the current 'on' began
    last_on: datetime | None  # last time it was on (now, if on)


def presence_active(rule: Rule, now: datetime, sensors: list[SensorPresence]) -> bool:
    if WEEKDAYS[now.weekday()] not in rule.presence_days:
        return False
    dwell = timedelta(minutes=rule.dwell_minutes)
    hold = timedelta(minutes=rule.hold_minutes)
    for s in sensors:
        if s.is_on and s.on_since is not None and now - s.on_since >= dwell:
            return True
    # Held on only after a qualifying stay: a walk-through never starts the hold. The
    # controller records `last_on` only for detections that met the dwell.
    return any(s.last_on is not None and now - s.last_on <= hold for s in sensors)


def rule_active(
    rule: Rule,
    when: datetime,
    now: datetime,
    in_window: bool,
    schedules: dict[str, WeeklyBlocks],
    presence_now: dict[str, bool],
) -> bool:
    if rule.trigger == "schedule":
        blocks = schedules.get(rule.schedule or "")
        return blocks is not None and blocks.covers(when)
    if rule.trigger == "presence":
        if in_window or not presence_now.get(rule.rule_id):
            return False
        # Assume people stay a while, so the AC runs proper blocks rather than chasing each
        # detection; never pre-condition for presence that hasn't happened yet.
        return now <= when <= now + PRESENCE_LOOKAHEAD
    return False


def resolve_band(
    tier: int,
    in_window: bool,
    tier_band: tuple[float, float],
    guard: tuple[float, float],
    rule_bands: list[tuple[float, float]],
) -> tuple[float, float]:
    if in_window and tier in (1, 2):
        return tier_band
    if rule_bands:
        lo, hi = max(b[0] for b in rule_bands), min(b[1] for b in rule_bands)
        # Rules that don't overlap can't both be met; fall back to the first rather than
        # handing the planner an impossible band.
        return (lo, hi) if lo < hi else rule_bands[0]
    if in_window:
        return tier_band
    return guard
