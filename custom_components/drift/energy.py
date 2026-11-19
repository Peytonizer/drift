"""AC power estimation and solar/grid split from a SolarEdge-style meter (pure Python).

Sign convention used internally: grid power > 0 means EXPORTING, < 0 importing.

* AC electrical power is learned from the step change on the AC's phase (or the
  whole meter) each time the system starts or stops.
* While the AC runs, the share covered by solar is whatever the house would have
  exported without it:  solar_used = clamp(grid + ac_w, 0, ac_w).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median

from .const import DEFAULT_AC_WATTS


@dataclass
class Transition:
    kind: str  # start | stop
    when: datetime


@dataclass
class AcPowerLearner:
    ac_watts: float = DEFAULT_AC_WATTS
    samples_learned: int = 0
    history: deque = field(default_factory=lambda: deque(maxlen=60))  # (time, phase_w export-positive)
    pending: list[Transition] = field(default_factory=list)
    steps: list[float] = field(default_factory=list)  # last measured steps (W)

    def add_sample(self, when: datetime, phase_w: float) -> None:
        self.history.append((when, phase_w))
        self._resolve(when)

    def mark(self, kind: str, when: datetime) -> None:
        self.pending.append(Transition(kind, when))

    def _window(self, start: datetime, end: datetime) -> list[float]:
        return [w for t, w in self.history if start <= t <= end]

    def _resolve(self, now: datetime) -> None:
        keep: list[Transition] = []
        for tr in self.pending:
            if now < tr.when + timedelta(minutes=9):
                keep.append(tr)
                continue
            before = self._window(tr.when - timedelta(minutes=6), tr.when)
            after = self._window(tr.when + timedelta(minutes=4), tr.when + timedelta(minutes=9))
            if len(before) >= 2 and len(after) >= 2:
                delta = median(before) - median(after)  # export-positive: AC start lowers it
                step = delta if tr.kind == "start" else -delta
                if 800 <= step <= 9000:
                    self.steps = (self.steps + [step])[-20:]
                    alpha = 1.0 if self.samples_learned == 0 else (0.3 if self.samples_learned < 5 else 0.1)
                    self.ac_watts = (1 - alpha) * self.ac_watts + alpha * step
                    self.samples_learned += 1
        self.pending = keep

    def to_dict(self) -> dict:
        return {"ac_watts": self.ac_watts, "samples_learned": self.samples_learned, "steps": self.steps}

    @classmethod
    def from_dict(cls, d: dict | None) -> "AcPowerLearner":
        x = cls()
        if d:
            x.ac_watts = float(d.get("ac_watts", DEFAULT_AC_WATTS))
            x.samples_learned = int(d.get("samples_learned", 0))
            x.steps = list(d.get("steps", []))
        return x


def split_solar_grid(ac_w: float, grid_w_export_positive: float | None) -> tuple[float, float]:
    """Return (solar_w, grid_w) used by the AC right now."""
    if ac_w <= 0:
        return 0.0, 0.0
    if grid_w_export_positive is None:
        return 0.0, ac_w
    solar = max(0.0, min(ac_w, grid_w_export_positive + ac_w))
    return solar, ac_w - solar


@dataclass
class EnergyTotals:
    ac_kwh: float = 0.0
    solar_kwh: float = 0.0
    grid_kwh: float = 0.0
    runtime_h: float = 0.0
    starts: int = 0

    def add(self, ac_w: float, solar_w: float, grid_w: float, hours: float) -> None:
        self.ac_kwh += ac_w * hours / 1000
        self.solar_kwh += solar_w * hours / 1000
        self.grid_kwh += grid_w * hours / 1000
        if ac_w > 0:
            self.runtime_h += hours

    def to_dict(self) -> dict:
        return dict(vars(self))

    @classmethod
    def from_dict(cls, d: dict | None) -> "EnergyTotals":
        x = cls()
        for k, v in (d or {}).items():
            if hasattr(x, k):
                setattr(x, k, v)
        return x
