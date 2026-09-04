"""The economist's own contribution, isolated: `naive_retry_3x`'s fixed
three-attempt ladder, priced and vetted the same way `retry_economist_prior.py`
prices `rules_only`'s plan - same `Economist`, same `HistoricalPriorEstimator`,
different plan source.

The point of pairing the economist with the DUMBEST plan source in the
project rather than the smartest one: `rules_only` already discriminates by
failure code before the economist ever sees a plan, so the gap between
`rules_only` and `retry_economist (prior)` understates what the economist
itself is worth - some of the good behaviour was already baked into the plan.
`naive_retry_3x` proposes the identical `(retry_now, retry_in_2h,
retry_in_24h)` ladder regardless of what failed, including on blocked cards
and closed accounts. Every improvement over `naive_retry_3x` this policy
shows - hard declines that stop consuming attempts, contact caps respected,
unprofitable retries vetoed - is attributable to the economist ALONE, because
the plan side did no discriminating at all.

See `retry_economist_prior.py`'s module docstring for the shared mechanics.
"""

from __future__ import annotations

from retry_economist.economist.estimator import HistoricalPriorEstimator
from retry_economist.economist.timing import DAILY_DISCOUNT_RATE
from retry_economist.policies.naive_retry import NaiveRetry3xPolicy
from retry_economist.policies.retry_economist_prior import EconomistOverPlan
from retry_economist.router.signals import SignalIndex


class RetryEconomistNaivePlanPolicy(EconomistOverPlan):
    """`naive_retry_3x`'s ladder, approved / truncated / vetoed by the economist."""

    name = "retry_economist (naive plan)"

    def __init__(
        self,
        index: SignalIndex,
        estimator: HistoricalPriorEstimator,
        *,
        daily_discount_rate: float = DAILY_DISCOUNT_RATE,
    ) -> None:
        self.plan_source = NaiveRetry3xPolicy()
        super().__init__(index, estimator, daily_discount_rate=daily_discount_rate)
