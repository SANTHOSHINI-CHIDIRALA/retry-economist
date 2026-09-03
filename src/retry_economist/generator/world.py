"""Issuers and their downtime — the environment every payment happens inside.

Downtime is modelled as a small number of windows per issuer rather than as
per-transaction noise, because that is what makes timing actions learnable: a
`retry_in_2h` can only beat `retry_now` if outages have *duration*. The windows
themselves are private to this module; every later stage sees the environment
exclusively through `World.issuer_health()`, so there is exactly one definition
of "was the bank sick at time t" in the codebase.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from random import Random
from typing import Literal

from retry_economist.schema import SIM_END, SIM_START

ReliabilityClass = Literal["stable", "flaky"]

#: Even a healthy issuer is never at a perfect 0 — a little background
#: degradation keeps `issuer_health` from being a boolean in disguise.
BASELINE_DEGRADATION: dict[ReliabilityClass, float] = {"stable": 0.01, "flaky": 0.04}

#: Fraction of a window spent ramping in and out. Real outages degrade and
#: recover gradually, which is what gives `retry_in_2h` a partial payoff when
#: fired into the tail of an incident.
_RAMP_FRACTION = 0.18


@dataclass(frozen=True, slots=True)
class DowntimeWindow:
    start: datetime
    end: datetime
    severity: float  # 0.0 = no impact, 1.0 = total outage

    def degradation_at(self, ts: datetime) -> float:
        """Trapezoidal severity profile: ramp in, plateau, ramp out."""
        if not (self.start <= ts < self.end):
            return 0.0
        span = (self.end - self.start).total_seconds()
        pos = (ts - self.start).total_seconds() / span
        ramp = _RAMP_FRACTION
        if pos < ramp:
            return self.severity * (pos / ramp)
        if pos > 1.0 - ramp:
            return self.severity * ((1.0 - pos) / ramp)
        return self.severity


@dataclass(frozen=True, slots=True)
class Issuer:
    code: str
    name: str
    base_success_rate: float
    reliability_class: ReliabilityClass
    downtime: tuple[DowntimeWindow, ...]


#: Realistic-looking IFSC-style issuer codes for eight Indian banks.
_ISSUER_SEEDS: tuple[tuple[str, str, ReliabilityClass], ...] = (
    ("HDFC", "HDFC Bank", "stable"),
    ("SBIN", "State Bank of India", "flaky"),
    ("ICIC", "ICICI Bank", "stable"),
    ("UTIB", "Axis Bank", "stable"),
    ("KKBK", "Kotak Mahindra Bank", "stable"),
    ("PYTM", "Paytm Payments Bank", "flaky"),
    ("BARB", "Bank of Baroda", "flaky"),
    ("YESB", "Yes Bank", "flaky"),
)

#: Traffic share. HDFC/SBI/ICICI dominate real Indian volume, and a skewed mix
#: matters: it decides how much of the dataset is exposed to flaky issuers.
ISSUER_SHARE: dict[str, float] = {
    "HDFC": 0.22,
    "SBIN": 0.20,
    "ICIC": 0.16,
    "UTIB": 0.13,
    "KKBK": 0.09,
    "PYTM": 0.08,
    "BARB": 0.07,
    "YESB": 0.05,
}


class World:
    """The issuer environment for one simulation run."""

    __slots__ = ("issuers", "start", "end")

    def __init__(self, issuers: dict[str, Issuer], start: datetime, end: datetime) -> None:
        self.issuers = issuers
        self.start = start
        self.end = end

    def issuer_health(self, issuer: str, ts: datetime) -> float:
        """Degradation score in [0, 1] for `issuer` at `ts`; 0 means healthy.

        Deliberately the *only* public view of downtime: transaction generation
        and the counterfactual oracle both call this, so neither can accidentally
        disagree about when a bank was down.
        """
        iss = self.issuers[issuer]
        worst = max((w.degradation_at(ts) for w in iss.downtime), default=0.0)
        baseline = BASELINE_DEGRADATION[iss.reliability_class]
        # Baseline and incident compose rather than add, so the score stays in [0, 1].
        return min(1.0, baseline + (1.0 - baseline) * worst)

    def codes(self) -> tuple[str, ...]:
        return tuple(self.issuers)


def _make_windows(rng: Random, klass: ReliabilityClass, start: datetime, end: datetime
                  ) -> tuple[DowntimeWindow, ...]:
    """Flaky issuers get 2-4 outages of 30min-6h; stable ones get 0-1."""
    count = rng.randint(2, 4) if klass == "flaky" else rng.randint(0, 1)
    total_minutes = int((end - start).total_seconds() // 60)
    windows: list[DowntimeWindow] = []
    for _ in range(count):
        duration = timedelta(minutes=rng.randint(30, 360))
        offset = rng.randrange(0, max(1, total_minutes - int(duration.total_seconds() // 60)))
        w_start = start + timedelta(minutes=offset)
        # Flaky issuers fail harder, not just more often.
        severity = rng.uniform(0.45, 0.98) if klass == "flaky" else rng.uniform(0.30, 0.75)
        windows.append(DowntimeWindow(w_start, w_start + duration, round(severity, 4)))
    windows.sort(key=lambda w: w.start)
    return tuple(windows)


def build_world(rng: Random, start: datetime = SIM_START, end: datetime = SIM_END) -> World:
    """Construct the 45-day issuer calendar from a seeded RNG."""
    issuers: dict[str, Issuer] = {}
    for code, name, klass in _ISSUER_SEEDS:
        base = rng.uniform(0.86, 0.96) if klass == "stable" else rng.uniform(0.86, 0.93)
        issuers[code] = Issuer(
            code=code,
            name=name,
            base_success_rate=round(base, 4),
            reliability_class=klass,
            downtime=_make_windows(rng, klass, start, end),
        )
    return World(issuers, start, end)


def world_report(world: World) -> list[dict[str, object]]:
    """Flat summary rows for the human-readable report."""
    return [
        {
            "issuer": i.code,
            "name": i.name,
            "base_success_rate": i.base_success_rate,
            "reliability_class": i.reliability_class,
            "downtime_windows": len(i.downtime),
            "downtime_hours": round(
                sum((w.end - w.start).total_seconds() for w in i.downtime) / 3600.0, 2
            ),
        }
        for i in world.issuers.values()
    ]
