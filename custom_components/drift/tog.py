"""Sleepwear TOG chart: TOG -> comfort band, and a bedtime recommendation (pure Python).

Default chart follows Love to Dream's Australian ratings:
    0.2 TOG  24-27 °C    1.0 TOG  20-24 °C    1.5 TOG  18-22 °C
    2.5 TOG  16-20 °C    3.5 TOG  below 16 °C (13-16 here, so it has a floor)
Every value is editable in the integration options.

The chart range is used as the band as-is. Where the AC aims when it does run comes from
the cool/heat depth (target = edge -/+ depth), so trimming the band as well would only make
it start earlier than the chart needs.
"""
from __future__ import annotations

from statistics import median

TOG_MANUAL = "Manual"
TOG_LEVELS = ["0.2", "1.0", "1.5", "2.5", "3.5"]
DEFAULT_CHART: dict[str, tuple[float, float]] = {
    "0.2": (24.0, 27.0),
    "1.0": (20.0, 24.0),
    "1.5": (18.0, 22.0),
    "2.5": (16.0, 20.0),
    "3.5": (13.0, 16.0),
}


def chart_from_options(opts: dict) -> dict[str, tuple[float, float]]:
    chart = {}
    for tog, (lo, hi) in DEFAULT_CHART.items():
        k = tog.replace(".", "_")
        chart[tog] = (float(opts.get(f"tog_{k}_min", lo)), float(opts.get(f"tog_{k}_max", hi)))
    return chart


def band_for(tog: str, chart: dict[str, tuple[float, float]]) -> tuple[float, float]:
    lo, hi = chart[tog]
    return round(lo, 1), round(hi, 1)


def recommend(temps: list[float], chart: dict[str, tuple[float, float]]) -> dict | None:
    """Pick the TOG whose chart range best covers the room's natural overnight temperatures.

    Choosing the suit that matches where the room would sit anyway means the AC
    has the least work to do.
    """
    if not temps:
        return None
    coverage = {}
    for tog, (lo, hi) in chart.items():
        coverage[tog] = sum(1 for t in temps if lo <= t <= hi) / len(temps)
    med = median(temps)

    def dist(tog: str) -> float:
        lo, hi = chart[tog]
        return 0.0 if lo <= med <= hi else min(abs(med - lo), abs(med - hi))

    def centre_gap(tog: str) -> float:
        lo, hi = chart[tog]
        return abs(med - (lo + hi) / 2)

    # Overlapping ranges (1.0 and 1.5 share 20-22) can tie on coverage; prefer the one
    # whose middle is closest to where the room will actually sit.
    best = sorted(chart, key=lambda tg: (-round(coverage[tg], 2), dist(tg), centre_gap(tg)))[0]
    return {
        "tog": best,
        "natural_min": round(min(temps), 1),
        "natural_max": round(max(temps), 1),
        "natural_median": round(med, 1),
        "coverage_pct": {k: round(v * 100) for k, v in coverage.items()},
    }
