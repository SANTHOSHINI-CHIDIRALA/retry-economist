"""The full architecture, end to end: the LLM router's plan, priced and vetted
by the SAME economist and estimator `retry_economist_prior.py` uses.

Phase 4 found the router's own `p_recover_if_act` / `p_recover_if_abstain`
LOSE to a train-only historical prior on both estimates (see
`docs/PROGRESS.md`, Phase 4). So the prior does the pricing here, exactly as
`retry_economist_prior.py` and `retry_economist_naive_plan.py` already price
`rules_only`'s and `naive_retry_3x`'s plans - the router's own self-reported
probabilities are never read. All this file adds is a third plan source; see
`retry_economist_prior.py`'s module docstring for the shared mechanics
(`EconomistOverPlan`), which this reuses unchanged.

Why this pairing is the one the other two are not: nothing in the router's
prompt tells it to abstain on a hard decline or a risk flag the way
`rules_only` does by construction, and `naive_retry_3x` never contacts a
customer at all - so this is the first plan source that can hand C1
(`RISK_DECLINED`), C2 (`HARD_DECLINE_NO_DEBIT`) and C5 (`CONTACT_CAP`) a plan
they might actually have something to remove from, and the first ordered,
mixed-action plan that gives `approve_truncated` a genuine partial removal to
truncate rather than an all-or-nothing one.

Runs entirely off the cache: the plan source is an `LLMRouterOnlyPolicy`
wrapping a `Router`, and the caller is responsible for handing it one backed
by a cache-only provider - this module makes no network-vs-cache decision of
its own.
"""

from __future__ import annotations

from retry_economist.economist.estimator import HistoricalPriorEstimator
from retry_economist.economist.timing import DAILY_DISCOUNT_RATE
from retry_economist.policies.llm_router_only import LLMRouterOnlyPolicy
from retry_economist.policies.retry_economist_prior import EconomistOverPlan
from retry_economist.router.router import Router
from retry_economist.router.signals import SignalIndex


class RetryEconomistLLMPlanPolicy(EconomistOverPlan):
    """The router's plan, approved / truncated / vetoed by the economist."""

    name = "retry_economist (LLM plan)"

    def __init__(
        self,
        index: SignalIndex,
        estimator: HistoricalPriorEstimator,
        router: Router,
        *,
        daily_discount_rate: float = DAILY_DISCOUNT_RATE,
    ) -> None:
        self.plan_source = LLMRouterOnlyPolicy(router)
        super().__init__(index, estimator, daily_discount_rate=daily_discount_rate)
