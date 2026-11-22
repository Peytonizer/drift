"""Sleep-safe watchdog: is the tier 1 room out of its band, whatever the planner thinks?

Pure and independent of the planner on purpose. The planner can be wrong (a bad forecast, a model
that has drifted, a bug, a dropped command); this only looks at the thermometer against the band
that was in force when each reading was taken, so a TOG change or the window starting mid-way
doesn't confuse it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

BIG_EXCESS_C = 1.0  # outside the band by more than this ...
BIG_MIN = 15  # ... for this many continuous minutes
SMALL_EXCESS_C = 0.5
SMALL_MIN = 45
CLEAR_MIN = 10  # back inside the band this long before an alert is cleared
HISTORY_MIN = 60  # how much of the series is worth keeping (the longest check is 45 minutes)
MAX_GAP_MIN = 5  # a gap in readings longer than this breaks "continuous"
STALE_READING_MIN = 10  # the newest reading is too old to say anything


@dataclass(frozen=True)
class Reading:
    when: datetime
    temp: float
    lo: float
    hi: float

    @property
    def excess(self) -> float:
        """Degrees outside the band: positive above, negative below, 0 inside."""
        if self.temp > self.hi:
            return self.temp - self.hi
        if self.temp < self.lo:
            return self.temp - self.lo
        return 0.0


@dataclass(frozen=True)
class Breach:
    side: str  # "above" or "below"
    level: str  # "severe" (over BIG_EXCESS_C for BIG_MIN) or "mild" (over SMALL_EXCESS_C for SMALL_MIN)
    excess: float  # the latest reading's distance outside the band, always positive
    minutes: float  # how long it has been beyond the threshold, continuously


def trim(series: list[Reading], now: datetime) -> list[Reading]:
    cutoff = now - timedelta(minutes=HISTORY_MIN)
    return [r for r in series if r.when >= cutoff]


def _run(series: list[Reading], threshold: float, sign: int) -> tuple[float, datetime | None]:
    """Minutes the newest readings have all been beyond `threshold` on one side, and when it began."""
    start = None
    prev: datetime | None = None
    for r in reversed(series):
        beyond = r.excess * sign > threshold
        if not beyond:
            break
        if prev is not None and (prev - r.when) > timedelta(minutes=MAX_GAP_MIN):
            break
        start, prev = r.when, r.when
    if start is None:
        return 0.0, None
    return (series[-1].when - start).total_seconds() / 60, start


def assess(series: list[Reading], now: datetime) -> Breach | None:
    """The breach in progress, if the newest readings meet either rule, else None."""
    if not series:
        return None
    series = sorted(series, key=lambda r: r.when)
    latest = series[-1]
    if now - latest.when > timedelta(minutes=STALE_READING_MIN):
        return None
    sign = 1 if latest.excess > 0 else -1 if latest.excess < 0 else 0
    if sign == 0:
        return None
    side = "above" if sign > 0 else "below"
    for threshold, minutes, level in ((BIG_EXCESS_C, BIG_MIN, "severe"), (SMALL_EXCESS_C, SMALL_MIN, "mild")):
        held, start = _run(series, threshold, sign)
        # a run that starts at the oldest reading we hold is only as long as we have watched it
        if start is not None and held >= minutes:
            return Breach(side, level, abs(latest.excess), held)
    return None


def back_in_band(series: list[Reading], now: datetime) -> bool:
    """True once the room has been inside its band for CLEAR_MIN minutes (so an alert may be cleared)."""
    if not series:
        return False
    window = [r for r in series if r.when >= now - timedelta(minutes=CLEAR_MIN)]
    if not window or any(r.excess != 0 for r in window):
        return False
    # we must actually have watched it for the full period, not just seen one good reading
    return window[0].when <= now - timedelta(minutes=CLEAR_MIN - 2)
