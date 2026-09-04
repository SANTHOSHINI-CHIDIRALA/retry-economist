"""Confidence intervals that respect how the data is actually clustered.

Resampling is done over CUSTOMERS, not transactions. One customer contributes
several failures, and those failures share everything that matters - the same
salary cycle, the same willingness to pay, the same dead card. Resampling
transactions would treat them as independent draws, understate the true variance
and produce intervals far tighter than the evidence supports. On a holdout of a
few hundred transactions belonging to a hundred-odd customers, that difference
is the difference between an honest interval and a flattering one.

The paired comparison matters just as much. Two policies scored on the same
customers move together: a bootstrap draw that happens to include several
hard-blocked payers drags BOTH policies down, and that shared swing is not
evidence about which policy is better. Resampling the same customer draw for
both and taking the difference inside each iteration cancels that shared noise,
so the interval on the difference is much tighter than anything obtainable by
eyeballing whether two independent intervals overlap - and overlapping marginal
intervals routinely hide a difference that is in fact significant.
"""

from __future__ import annotations

from dataclasses import dataclass
from random import Random
from typing import Callable, Sequence

from retry_economist.eval.costs import (
    CUSTOMER_LIFETIME_VALUE_PAISE,
    annoyance_to_paise,
    recovered_value_paise,
)
from retry_economist.eval.simulator import RunResult, TxnOutcome

DEFAULT_ITERATIONS = 2000
DEFAULT_SEED = 20260601


@dataclass(frozen=True, slots=True)
class ConfidenceInterval:
    """A point estimate with a percentile interval around it."""

    point: float | None
    low: float | None
    high: float | None
    level: float = 0.95

    def contains(self, value: float) -> bool:
        if self.low is None or self.high is None:
            return False
        return self.low <= value <= self.high

    def to_dict(self) -> dict[str, float | None]:
        return {
            "point": None if self.point is None else round(self.point, 6),
            "low": None if self.low is None else round(self.low, 6),
            "high": None if self.high is None else round(self.high, 6),
            "level": self.level,
        }

    def render(self, *, pct: bool = False, places: int = 2) -> str:
        if self.point is None:
            return "n/a"
        scale = 100.0 if pct else 1.0
        suffix = "%" if pct else ""
        if self.low is None or self.high is None:
            return f"{self.point * scale:.{places}f}{suffix}"
        return (
            f"{self.point * scale:.{places}f}{suffix} "
            f"[{self.low * scale:.{places}f}, {self.high * scale:.{places}f}]"
        )


@dataclass(frozen=True, slots=True)
class _CustomerAggregate:
    """One customer's totals, so an iteration is a sum instead of a re-scoring.

    Every statistic below is a ratio of sums, so pre-aggregating per cluster
    makes 2000 iterations cost a few hundred thousand additions rather than
    2000 full metric computations.
    """

    n: int
    recovered: int
    would_pay_anyway: int
    incremental_paise: int
    cannibalised_paise: int
    cost_paise: int
    annoyance_units: float
    #: Confusion-matrix cells for the act/abstain decision, so decision quality
    #: can be resampled from the same clusters as everything else.
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    recovery_rate: ConfidenceInterval
    net_uplift_pp: ConfidenceInterval
    cost_per_incremental_rupee: ConfidenceInterval
    iterations: int
    seed: int
    n_customers: int

    def to_dict(self) -> dict[str, object]:
        return {
            "iterations": self.iterations,
            "seed": self.seed,
            "n_customers": self.n_customers,
            "recovery_rate": self.recovery_rate.to_dict(),
            "net_uplift_pp": self.net_uplift_pp.to_dict(),
            "cost_per_incremental_rupee": self.cost_per_incremental_rupee.to_dict(),
        }


def _aggregate_by_customer(outcomes: Sequence[TxnOutcome]) -> dict[str, _CustomerAggregate]:
    acc: dict[str, list] = {}
    for o in outcomes:
        slot = acc.setdefault(o.customer_id, [0, 0, 0, 0, 0, 0, 0.0, 0, 0, 0, 0])
        slot[0] += 1
        slot[1] += int(o.recovered)
        slot[2] += int(o.would_pay_anyway)
        if o.incremental:
            slot[3] += recovered_value_paise(o.amount_paise)
        if o.cannibalised:
            slot[4] += recovered_value_paise(o.amount_paise)
        slot[5] += o.total_cost_paise
        slot[6] += o.annoyance_delta
        slot[7] += int(o.incremental)
        slot[8] += int(o.wasted or o.futile or o.cannibalised)
        slot[9] += int(o.missed_opportunity)
        slot[10] += int(o.correct_restraint or o.correct_walkaway)
    return {
        cid: _CustomerAggregate(
            n=v[0],
            recovered=v[1],
            would_pay_anyway=v[2],
            incremental_paise=v[3],
            cannibalised_paise=v[4],
            cost_paise=v[5],
            annoyance_units=v[6],
            tp=v[7],
            fp=v[8],
            fn=v[9],
            tn=v[10],
        )
        for cid, v in acc.items()
    }


def _statistics(
    sample: Sequence[_CustomerAggregate],
    clv_paise: int = CUSTOMER_LIFETIME_VALUE_PAISE,
) -> tuple[float, float, float | None]:
    """(recovery_rate, net_uplift_pp, cost_per_incremental_rupee) for one draw."""
    n = sum(a.n for a in sample)
    if n == 0:
        return 0.0, 0.0, None
    recovered = sum(a.recovered for a in sample)
    organic = sum(a.would_pay_anyway for a in sample)
    net_paise = sum(a.incremental_paise for a in sample) - sum(a.cannibalised_paise for a in sample)
    spend_paise = sum(a.cost_paise for a in sample) + annoyance_to_paise(
        sum(a.annoyance_units for a in sample), clv_paise=clv_paise
    )
    recovery_rate = recovered / n
    net_uplift_pp = (recovered - organic) / n * 100.0
    cost_ratio = spend_paise / net_paise if net_paise > 0 else None
    return recovery_rate, net_uplift_pp, cost_ratio


def net_uplift_statistic(sample: Sequence[_CustomerAggregate]) -> float:
    """Net uplift in percentage points for one resampled cluster set."""
    return _statistics(sample)[1]


def decision_f1_statistic(sample: Sequence[_CustomerAggregate]) -> float:
    """Decision F1 for one resampled cluster set.

    Returns 0.0 rather than None for a draw in which the policy never acted:
    inside a bootstrap the value has to be a number, and a policy that acted on
    nothing genuinely achieved no true positives in that draw.
    """
    tp = sum(a.tp for a in sample)
    fp = sum(a.fp for a in sample)
    fn = sum(a.fn for a in sample)
    if tp == 0:
        return 0.0
    precision = tp / (tp + fp)
    recall = tp / (tp + fn)
    return 2 * precision * recall / (precision + recall)


def _percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile on a pre-sorted sequence."""
    if not values:
        raise ValueError("no values")
    idx = min(len(values) - 1, max(0, int(round(q * (len(values) - 1)))))
    return values[idx]


def _interval(samples: list[float], point: float | None, level: float) -> ConfidenceInterval:
    if not samples or point is None:
        return ConfidenceInterval(point=point, low=None, high=None, level=level)
    samples.sort()
    tail = (1.0 - level) / 2.0
    return ConfidenceInterval(
        point=point,
        low=_percentile(samples, tail),
        high=_percentile(samples, 1.0 - tail),
        level=level,
    )


def bootstrap_run(
    result: RunResult,
    *,
    iterations: int = DEFAULT_ITERATIONS,
    seed: int = DEFAULT_SEED,
    level: float = 0.95,
    clv_paise: int = CUSTOMER_LIFETIME_VALUE_PAISE,
) -> BootstrapResult:
    """Clustered percentile CIs for one policy's run."""
    by_customer = _aggregate_by_customer(result.outcomes)
    # Sorted so the draw sequence depends on the seed alone, never on the order
    # transactions happened to arrive in.
    customers = sorted(by_customer)
    aggregates = [by_customer[c] for c in customers]
    point_recovery, point_uplift, point_cost = _statistics(aggregates, clv_paise)

    rng = Random(seed)
    k = len(customers)
    recovery_samples: list[float] = []
    uplift_samples: list[float] = []
    cost_samples: list[float] = []

    for _ in range(iterations):
        draw = [aggregates[rng.randrange(k)] for _ in range(k)]
        r, u, c = _statistics(draw, clv_paise)
        recovery_samples.append(r)
        uplift_samples.append(u)
        if c is not None:
            # Iterations with no net revenue contribute no meaningful ratio;
            # dropping them narrows the interval to the draws where the
            # statistic is defined, and the point estimate says whether it is.
            cost_samples.append(c)

    return BootstrapResult(
        recovery_rate=_interval(recovery_samples, point_recovery, level),
        net_uplift_pp=_interval(uplift_samples, point_uplift, level),
        cost_per_incremental_rupee=_interval(cost_samples, point_cost, level),
        iterations=iterations,
        seed=seed,
        n_customers=k,
    )


def paired_bootstrap(
    run_a: RunResult,
    run_b: RunResult,
    *,
    iterations: int = DEFAULT_ITERATIONS,
    seed: int = DEFAULT_SEED,
    level: float = 0.95,
    statistic: Callable[[Sequence[_CustomerAggregate]], float] | None = None,
) -> ConfidenceInterval:
    """CI for (run_a - run_b) in net uplift, resampling the same customers for both.

    Both runs must cover the same customers; the whole point is that each
    iteration draws one customer list and evaluates both policies on it, so
    customer-composition noise cancels instead of being counted twice.
    """
    agg_a = _aggregate_by_customer(run_a.outcomes)
    agg_b = _aggregate_by_customer(run_b.outcomes)
    customers = sorted(set(agg_a) & set(agg_b))
    if not customers:
        return ConfidenceInterval(point=None, low=None, high=None, level=level)

    missing = (set(agg_a) | set(agg_b)) - set(customers)
    if missing:
        raise ValueError(
            f"paired bootstrap needs identical customer sets; {len(missing)} differ "
            f"(e.g. {sorted(missing)[:3]})"
        )

    pick = statistic or (lambda sample: _statistics(sample)[1])
    list_a = [agg_a[c] for c in customers]
    list_b = [agg_b[c] for c in customers]
    point = pick(list_a) - pick(list_b)

    rng = Random(seed)
    k = len(customers)
    diffs: list[float] = []
    for _ in range(iterations):
        idx = [rng.randrange(k) for _ in range(k)]
        diffs.append(pick([list_a[i] for i in idx]) - pick([list_b[i] for i in idx]))

    return _interval(diffs, point, level)
