"""Pure tests for the sleep-safe watchdog."""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "drift"
spec = importlib.util.spec_from_file_location("drift_watchdog", ROOT / "watchdog.py")
wd = importlib.util.module_from_spec(spec)
import sys  # noqa: E402

sys.modules["drift_watchdog"] = wd  # dataclasses look the module up by name
spec.loader.exec_module(wd)

AEDT = timezone(timedelta(hours=11))
T0 = datetime(2026, 11, 22, 22, 0, tzinfo=AEDT)
LO, HI = 24.0, 27.0


def series(temps, start=T0, step=1, lo=LO, hi=HI):
    return [wd.Reading(start + timedelta(minutes=i * step), t, lo, hi) for i, t in enumerate(temps)]


def test_inside_the_band_is_fine():
    s = series([25.0] * 60)
    assert wd.assess(s, s[-1].when) is None


def test_severe_needs_over_a_degree_for_fifteen_minutes():
    s = series([28.2] * 16)  # 1.2 over, 15 minutes of readings (0..15)
    b = wd.assess(s, s[-1].when)
    assert b and b.level == "severe" and b.side == "above" and abs(b.excess - 1.2) < 1e-9
    s = series([28.2] * 14)  # only 13 minutes
    assert wd.assess(s, s[-1].when) is None


def test_exactly_a_degree_over_is_not_severe():
    s = series([28.0] * 30)  # 1.0 over: not "more than 1.0"
    b = wd.assess(s, s[-1].when)
    assert b is None  # and only 29 minutes past 0.5, short of the 45 for mild


def test_mild_needs_over_half_a_degree_for_forty_five_minutes():
    s = series([27.7] * 46)
    b = wd.assess(s, s[-1].when)
    assert b and b.level == "mild"
    s = series([27.7] * 40)
    assert wd.assess(s, s[-1].when) is None


def test_below_the_band_is_a_breach_too():
    s = series([22.5] * 16)  # 1.5 under
    b = wd.assess(s, s[-1].when)
    assert b and b.side == "below" and b.level == "severe"


def test_a_dip_back_inside_resets_the_clock():
    s = series([28.5] * 10 + [26.0] + [28.5] * 10)
    assert wd.assess(s, s[-1].when) is None  # only 10 continuous minutes since the dip


def test_switching_sides_resets_the_clock():
    s = series([22.5] * 10 + [28.5] * 10)
    assert wd.assess(s, s[-1].when) is None


def test_a_gap_in_readings_breaks_continuity():
    a = series([28.5] * 8)
    b = series([28.5] * 9, start=T0 + timedelta(minutes=30))
    s = a + b
    assert wd.assess(s, s[-1].when) is None


def test_stale_series_says_nothing():
    s = series([28.5] * 30)
    assert wd.assess(s, s[-1].when + timedelta(minutes=11)) is None


def test_each_reading_is_judged_against_the_band_it_was_taken_under():
    # the band was widened (TOG change) halfway: the earlier readings were out, the later are inside
    s = series([27.8] * 10, hi=27.0) + series([27.8] * 10, start=T0 + timedelta(minutes=10), hi=29.0)
    assert wd.assess(s, s[-1].when) is None


def test_clear_needs_ten_minutes_inside():
    s = series([28.5] * 5 + [25.0] * 5)
    assert not wd.back_in_band(s, s[-1].when)
    s = series([28.5] * 5 + [25.0] * 12)
    assert wd.back_in_band(s, s[-1].when)
    s = series([25.0] * 3)  # one good stretch isn't enough: we haven't watched it for ten minutes
    assert not wd.back_in_band(s, s[-1].when)


def test_trim_keeps_the_last_hour():
    s = series([25.0] * 90)
    kept = wd.trim(s, s[-1].when)
    assert kept[0].when >= s[-1].when - timedelta(minutes=60) and kept[-1] is s[-1]
