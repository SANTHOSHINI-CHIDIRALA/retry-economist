"""The full system, without a language model: `rules_only`'s plan, priced and
vetted by the economist against the train-only historical prior.

This exists because Phase 5's economist needs a PLAN to evaluate, and evaluating
one does not require that plan to have come from an LLM. `rules_only` already
proposes a deterministic, observed-data-only plan for every transaction; this
policy reuses that plan unchanged and adds what `rules_only` cannot: an
incremental expected-value gate and five hard compliance rules, priced with
`HistoricalPriorEstimator` rather than a model's self-reported probabilities.

Two consequences follow directly from that choice:

- it needs no API key, no cache, and no network - it can score the entire
  holdout in the time a lookup table takes;
- it is a genuine test of the ECONOMIST layer in isolation from the ROUTER
  layer, because the plan side is held fixed at `rules_only`'s own proposal.
  Any difference between this policy and plain `rules_only` is attributable
  to the economics, not to a better plan.

`llm_router_only`'s eventual full-system counterpart - the router's plan
priced the same way - is deferred until the LLM run lands (see
`docs/PROGRESS.md`, Phase 4/5); wiring that in only means swapping which
object supplies `proposed_plan` and `signals`, not touching this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence, Tuple

from retry_economist.economist import EconomistDecision, Economist
from retry_economist.economist.estimator import HistoricalPriorEstimator
from retry_economist.economist.timing import DAILY_DISCOUNT_RATE
from retry_economist.policies.base import Decision, ObservedTransaction
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


class RetryEconomistPriorPolicy:
    """`rules_only`'s plan, approved / truncated / vetoed by the economist."""

    name = "retry_economist (prior)"

    def __init__(
        self,
        index: SignalIndex,
        estimator: HistoricalPriorEstimator,
        *,
        daily_discount_rate: float = DAILY_DISCOUNT_RATE,
    ) -> None:
        self.index = index
        self.rules = RulesOnlyPolicy()
        self.economist = Economist(estimator, daily_discount_rate=daily_discount_rate)
        #: Kept so the report can audit individual decisions without
        #: re-deciding them - same pattern as `LLMRouterOnlyPolicy.proposals`.
        self.decisions: dict[str, EconomistDecision] = {}

    def decide(self, txn: ObservedTransaction) -> Decision:
        rule_decision = self.rules.decide(txn)
        signals = self.index.signals_for(txn)
        proposal = _PlanProposal(proposed_plan=tuple(rule_decision.plan), signals=signals)

        economist_decision = self.economist.decide(txn, proposal)
        self.decisions[txn.txn_id] = economist_decision

        return Decision(
            plan=list(economist_decision.plan),
            reason=economist_decision.reason,
            metadata={
                "verdict": economist_decision.verdict,
                "proposed_plan": list(economist_decision.proposed_plan),
                "rule_reason": rule_decision.reason,
                "ev": None if economist_decision.ev is None else economist_decision.ev.to_dict(),
                "compliance": economist_decision.compliance.to_dict(),
            },
        )
