"""Customers: a thin observable shell around rich latent state.

The router will only ever see the shell. The latent half - salary cycle,
liquidity curve, true intent, accumulated annoyance, hard-block status - is what
actually decides whether a payment recovers, and it is exactly what a real PSP
does not have. That gap is the research problem; this module manufactures it on
purpose.

Liquidity is a *continuous function of time*, not a stored scalar, because the
oracle has to evaluate it at arbitrary future instants (two hours from now, next
salary day) to price timing actions. It is also deterministic: given the same
customer, the same timestamp always yields the same liquidity, with no RNG
draws, so counterfactuals never depend on evaluation order.
"""

from __future__ import annotations

import hashlib
import math
from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta
from random import Random
from typing import Any

from retry_economist.schema import IST, CityTier, Method

#: Salary hits the account mid-morning, not at midnight - a retry fired at 02:00
#: on payday should still find the account dry.
SALARY_CREDIT_HOUR = 10

_PEAK_LIQUIDITY = 0.94
_DECAY_K = 2.2  # curvature of the post-salary drawdown


def stable_unit(*parts: object) -> float:
    """Deterministic pseudo-random float in [0, 1) from arbitrary keys.

    Python's builtin hash() is salted per process, so anything that must stay
    reproducible across runs (liquidity jitter, train/holdout splits) hashes
    explicitly instead.
    """
    key = "|".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "big") / 2.0**64


def clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _salary_datetime(year: int, month: int, salary_day: int) -> datetime:
    """Payday for a given month, clamped into short months (a '30' in February)."""
    day = min(salary_day, monthrange(year, month)[1])
    return datetime(year, month, day, SALARY_CREDIT_HOUR, tzinfo=IST)


def _month_shift(ts: datetime, months: int) -> tuple[int, int]:
    index = ts.year * 12 + (ts.month - 1) + months
    return index // 12, index % 12 + 1


@dataclass(frozen=True, slots=True)
class Customer:
    """One payer. Everything from `salary_day` down is invisible to the agent."""

    # --- observable -------------------------------------------------------
    customer_id: str
    tenure_days: int
    past_txn_count: int
    past_success_rate: float
    prior_failed_attempts_this_invoice: int
    comms_received_last_7d: int
    preferred_method: Method
    city_tier: CityTier

    # --- latent -----------------------------------------------------------
    salary_day: int | None  # None => gig / irregular income
    intent_to_pay: float
    annoyance: float
    hard_blocked: bool
    liquidity_floor: float  # personal cash buffer; wealthy payers never hit zero

    def liquidity(self, ts: datetime) -> float:
        """Available-balance proxy in [0, 1] at `ts`.

        Salaried payers peak just after payday and grind down to a trough right
        before the next one - the single biggest cause of INSUFFICIENT_FUNDS,
        and the reason `retry_next_salary_day` can exist as a strategy at all.
        """
        jitter = (stable_unit(self.customer_id, ts.date(), "liq") - 0.5) * 0.10

        if self.salary_day is None:
            return clamp01(self._gig_liquidity(ts) + jitter)

        last, nxt = self.salary_bracket(ts)
        frac = (ts - last).total_seconds() / (nxt - last).total_seconds()

        trough = self.liquidity_floor
        peak = max(trough + 0.05, _PEAK_LIQUIDITY * (0.75 + 0.35 * self.liquidity_floor))
        # Normalised exponential decay, so the curve lands exactly on `trough`
        # at the end of the cycle whatever the cycle length happens to be.
        shape = (math.exp(-_DECAY_K * frac) - math.exp(-_DECAY_K)) / (1.0 - math.exp(-_DECAY_K))
        return clamp01(trough + (peak - trough) * shape + jitter)

    def salary_bracket(self, ts: datetime) -> tuple[datetime, datetime]:
        """Paydays bracketing `ts`, handling the wrap across month boundaries."""
        assert self.salary_day is not None
        this_month = _salary_datetime(ts.year, ts.month, self.salary_day)
        if ts >= this_month:
            ny, nm = _month_shift(ts, 1)
            return this_month, _salary_datetime(ny, nm, self.salary_day)
        py, pm = _month_shift(ts, -1)
        return _salary_datetime(py, pm, self.salary_day), this_month

    def _gig_liquidity(self, ts: datetime) -> float:
        """Erratic income: a smooth walk interpolated between daily draws.

        Continuous, so a two-hour delay is a small change, but with no payday
        structure at all - there is nothing here for a timing policy to exploit.
        """
        day = ts.date()
        today = stable_unit(self.customer_id, day, "gig")
        tomorrow = stable_unit(self.customer_id, day + timedelta(days=1), "gig")
        midnight = datetime(day.year, day.month, day.day, tzinfo=ts.tzinfo)
        frac = (ts - midnight).total_seconds() / 86400.0
        blended = today + (tomorrow - today) * frac
        return self.liquidity_floor + (0.85 - self.liquidity_floor) * blended


_METHOD_MIX: tuple[tuple[Method, float], ...] = (
    ("upi_collect", 0.16),
    ("upi_intent", 0.24),
    ("card", 0.24),
    ("netbanking", 0.08),
    ("upi_autopay", 0.19),
    ("enach", 0.09),
)

#: Most Indian payrolls land on the 1st, the 7th or month-end; a long tail of
#: irregular dates plus a real gig cohort keeps the salary signal from being a
#: three-way lookup.
_SALARY_DAY_MIX: tuple[tuple[int | None, float], ...] = (
    (1, 0.32),
    (7, 0.18),
    (30, 0.17),
    (5, 0.05),
    (10, 0.05),
    (15, 0.04),
    (25, 0.04),
    (None, 0.15),
)


def _weighted(rng: Random, mix: tuple[tuple[Any, float], ...]) -> Any:
    return rng.choices([v for v, _ in mix], weights=[w for _, w in mix], k=1)[0]


def build_customers(rng: Random, n: int = 300) -> dict[str, Customer]:
    """Generate `n` customers, keyed by id, in stable insertion order."""
    customers: dict[str, Customer] = {}
    for i in range(n):
        cid = f"cust_{i:04d}"

        # Hard blocks are rare but decisive: no retry will ever recover them,
        # which is what makes "stop spending money on this one" a real decision.
        hard_blocked = rng.random() < 0.07

        # Intent is bimodal: most people mean to pay, a minority have quietly
        # decided not to. A unimodal draw would make nudges uniformly mediocre
        # instead of sharply right for a specific subpopulation.
        if rng.random() < 0.76:
            intent = rng.uniform(0.58, 0.99)
        else:
            intent = rng.uniform(0.04, 0.52)
        if hard_blocked:
            intent = min(intent, rng.uniform(0.15, 0.70))

        salary_day = _weighted(rng, _SALARY_DAY_MIX)
        liquidity_floor = (
            rng.uniform(0.05, 0.45) if salary_day is not None else rng.uniform(0.03, 0.30)
        )

        tenure_days = int(rng.triangular(5, 1400, 220))
        # Volume follows tenure with heavy noise, so tenure is informative
        # without being a proxy the router can lean on blindly.
        past_txn_count = max(1, int(tenure_days / 30.0 * rng.uniform(0.4, 2.6)))

        # Observable history leaks latent state only weakly and noisily - this
        # is the router's main honest signal, and it is meant to be imperfect.
        base_history = 0.70 + 0.22 * intent + 0.12 * liquidity_floor
        if hard_blocked:
            base_history -= 0.10
        past_success_rate = clamp01(base_history + rng.gauss(0.0, 0.06))

        comms = rng.choices([0, 1, 2, 3, 4, 5], weights=[30, 26, 18, 12, 8, 6], k=1)[0]
        prior_failed = rng.choices([0, 1, 2, 3], weights=[52, 28, 14, 6], k=1)[0]
        if hard_blocked or intent < 0.30:
            prior_failed = min(3, prior_failed + rng.choice([0, 1, 1]))

        customers[cid] = Customer(
            customer_id=cid,
            tenure_days=tenure_days,
            past_txn_count=past_txn_count,
            past_success_rate=round(past_success_rate, 4),
            prior_failed_attempts_this_invoice=prior_failed,
            comms_received_last_7d=comms,
            preferred_method=_weighted(rng, _METHOD_MIX),
            city_tier=rng.choices([1, 2, 3], weights=[0.44, 0.34, 0.22], k=1)[0],
            salary_day=salary_day,
            intent_to_pay=round(intent, 4),
            annoyance=round(clamp01(0.05 * comms + 0.06 * prior_failed + rng.uniform(0.0, 0.08)), 4),
            hard_blocked=hard_blocked,
            liquidity_floor=round(liquidity_floor, 4),
        )
    return customers


def next_salary_credit(customer: Customer, after: datetime) -> datetime | None:
    """First payday strictly after `after`, or `None` for gig workers.

    `None` is a meaningful answer, not an error: it is exactly why
    `retry_next_salary_day` has to degrade gracefully for irregular earners.
    """
    if customer.salary_day is None:
        return None
    return customer.salary_bracket(after)[1]


def customer_public_fields(customer: Customer) -> dict[str, Any]:
    """The half of a customer a payment gateway would actually have on file."""
    return {
        "tenure_days": customer.tenure_days,
        "past_txn_count": customer.past_txn_count,
        "past_success_rate": customer.past_success_rate,
        "prior_failed_attempts_this_invoice": customer.prior_failed_attempts_this_invoice,
        "comms_received_last_7d": customer.comms_received_last_7d,
        "preferred_method": customer.preferred_method,
        "city_tier": customer.city_tier,
    }
