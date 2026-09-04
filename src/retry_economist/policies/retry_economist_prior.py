"""The full system, without a language model: a deterministic plan, priced
and vetted by the economist against the train-only historical prior.

This exists because Phase 5's economist needs a PLAN to evaluate, and evaluating
one does not require that plan to have come from an LLM. Any deterministic,
observed-data-only policy can supply the plan side; this module reuses one
unchanged and adds what it cannot do on its own: an incremental expected-value
gate and five hard compliance rules, priced with `HistoricalPriorEstimator`
rather than a model's self-reported probabilities.

Two consequences follow directly from that choice:

- it needs no API key, no cache, and no network - it can score the entire
  holdout in the time a lookup table takes;
- it is a genuine test of the ECONOMIST layer in isolation from the PLAN
  layer, because the plan side is held fixed at the source policy's own
  proposal. Any difference between the combined policy and its plan source is
  attributable to the economics, not to a better plan.

Two plan sources are wired up, in this file and in
`retry_economist_naive_plan.py`:

- `RetryEconomistPriorPolicy` prices `rules_only`'s plan - a plan source that
  already discriminates by failure code, so the economist's marginal
  contribution over it is comparatively small.
- `RetryEconomistNaivePlanPolicy` prices `naive_retry_3x`'s fixed three-attempt
  ladder instead - a plan source that proposes the SAME retries regardless of
  what failed, so the economist has to do all of the discriminating on its
  own. The gap between naive_retry_3x and this policy is a clean measurement
  of what the economist alone is worth, uncontaminated by rules_only's
  own domain knowledge.

`llm_router_only`'s eventual full-system counterpart - the router's plan
priced the same way - is deferred until the LLM run lands (see
`docs/PROGRESS.md`, Phase 4/5); wiring that in only means adding a third
plan source below, not touching the economist call itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from retry_economist.economist import EconomistDecision, Economist
from retry_economist.economist.estimator import HistoricalPriorEstimator
from retry_economist.economist.timing import DAILY_DISCOUNT_RATE
from retry_economist.policies.base import Decision, ObservedTransaction, Policy
from retry_economist.policies.rules_only import RulesOnlyPolicy
from retry_economist.router.signals import Signals, SignalIndex


@dataclass(frozen=True, slots=True)
class _PlanProposal:
    """Just enough of `router.router.Proposal`'s shape for the economist to
    read: a plan, and the deterministic signals that time it. No probability
    fields, because `HistoricalPriorEstimator` never reads them - see
    `estimator.py`."""

    proposed_plan: Tuple[str, ...]
    signals: Signals


class EconomistOverPlan:
    """Shared machinery: run `plan_source`, price its plan through the
    economist. `name` and `plan_source` are set by the two concrete
    subclasses below, each in its own module per this codebase's one-file-per-
    named-policy convention.
    """

    name: str
    plan_source: Policy

    def __init__(
        self,
        index: SignalIndex,
        estimator: HistoricalPriorEstimator,
        *,
        daily_discount_rate: float = DAILY_DISCOUNT_RATE,
    ) -> None:
        self.index = index
        self.economist = Economist(estimator, daily_discount_rate=daily_discount_rate)
        #: Kept so the report can audit individual decisions without
        #: re-deciding them - same pattern as `LLMRouterOnlyPolicy.proposals`.
        self.decisions: dict[str, EconomistDecision] = {}

    def decide(self, txn: ObservedTransaction) -> Decision:
        source_decision = self.plan_source.decide(txn)
        signals = self.index.signals_for(txn)
        proposal = _PlanProposal(proposed_plan=tuple(source_decision.plan), signals=signals)

        economist_decision = self.economist.decide(txn, proposal)
        self.decisions[txn.txn_id] = economist_decision

        return Decision(
            plan=list(economist_decision.plan),
            reason=economist_decision.reason,
            metadata={
                "verdict": economist_decision.verdict,
                "proposed_plan": list(economist_decision.proposed_plan),
                "plan_source_reason": source_decision.reason,
                "ev": None if economist_decision.ev is None else economist_decision.ev.to_dict(),
                "compliance": economist_decision.compliance.to_dict(),
            },
        )


class RetryEconomistPriorPolicy(EconomistOverPlan):
    """`rules_only`'s plan, approved / truncated / vetoed by the economist."""

    name = "retry_economist (prior)"

    def __init__(
        self,
        index: SignalIndex,
        estimator: HistoricalPriorEstimator,
        *,
        daily_discount_rate: float = DAILY_DISCOUNT_RATE,
    ) -> None:
        self.plan_source = RulesOnlyPolicy()
        super().__init__(index, estimator, daily_discount_rate=daily_discount_rate)
