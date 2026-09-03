"""Executes a policy's plans against the counterfactual store and records what
happened, one transaction at a time.

Two design choices carry most of the weight.

The compliance gate lives HERE, not in the policy. Scheme and mandate rules cap
debit attempts per invoice, and a system that trusts each policy to police its
own cap has no way to tell a compliant policy from a lucky one. The harness
meters every plan, truncates what exceeds the budget, and reports the breach
loudly - a policy with any violation is marked INVALID on the scoreboard rather
than being quietly corrected and scored as if it had behaved.

A plan that executes nothing - whether the policy abstained or the gate
truncated it away - resolves to the no-intervention counterfactual. That is what
makes abstaining a real, scoreable choice: the customer is left alone and may well pay
unaided. A non-empty plan does NOT fall back to it, and that asymmetry is the
point - intervening replaces what would have happened, so a policy that acts on
someone who was about to pay anyway can genuinely destroy the recovery. Those
show up as `cannibalised` in the metrics.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from retry_economist.eval.costs import action_cost
from retry_economist.policies.base import ObservedTransaction, Policy, validated_decision
from retry_economist.schema import ACTIONS, ATTEMPTS_CONSUMED

#: The action name the counterfactual store uses for "left alone".
_NULL_OUTCOME = "do_nothing"


@dataclass(frozen=True, slots=True)
class ComplianceViolation:
    """A plan that asked for more debit attempts than the invoice allows."""

    txn_id: str
    policy_name: str
    attempts_requested: int
    attempts_allowed: int
    plan: tuple[str, ...]
    executed: tuple[str, ...]

    def describe(self) -> str:
        return (
            f"{self.txn_id}: plan {list(self.plan)} requests {self.attempts_requested} "
            f"debit attempts but only {self.attempts_allowed} remain"
        )


@dataclass(frozen=True, slots=True)
class TxnOutcome:
    """Everything the metrics and bootstrap layers need about one transaction.

    Deliberately self-contained: once a run is over, every number in the report
    is recomputable from these records without invoking a policy again, which is
    what lets 2000 bootstrap iterations cost milliseconds instead of hours.
    """

    txn_id: str
    customer_id: str
    amount_paise: int
    failure_code: str
    decline_type: str
    recovered: bool
    would_pay_anyway: bool
    attempts_used: int
    plan: tuple[str, ...]
    actions_executed: tuple[str, ...]
    total_cost_paise: int
    annoyance_delta: float
    contacted_customer: bool
    compliance_violation: bool
    reason: str
    decide_seconds: float
    #: Hours from the original failure to the money arriving, taken from the
    #: action that actually recovered it. None when nothing recovered. Carried
    #: through because a policy can win on recovery rate purely by waiting, and
    #: the scoreboard has to be able to see that.
    hours_to_recovery: float | None = None
    #: Would some action OTHER than abstaining have recovered this, given the
    #: debit attempts the invoice actually had left? Recorded at execution time
    #: because only the harness may consult the counterfactual store - it is what
    #: separates a correct walk-away from a missed opportunity.
    recoverable_within_caps: bool = False
    #: The same question ignoring attempt caps. Reported for context: an action
    #: the mandate forbids is not an opportunity anyone could have taken.
    recoverable_ignoring_caps: bool = False

    @property
    def acted(self) -> bool:
        """Did anything actually happen to this customer?

        False when the policy abstained AND when the compliance gate truncated
        its plan to nothing - in both cases the customer was left alone, which
        is what the abstention buckets are about.
        """
        return bool(self.actions_executed)

    # --- buckets for transactions we ACTED on ------------------------------

    @property
    def incremental(self) -> bool:
        """Recovered because of us. The only bucket that is actually revenue."""
        return self.acted and self.recovered and not self.would_pay_anyway

    @property
    def cannibalised(self) -> bool:
        """Would have paid unaided; our intervention lost it."""
        return self.acted and self.would_pay_anyway and not self.recovered

    @property
    def wasted(self) -> bool:
        """Recovered, but would have anyway. We paid for a foregone conclusion."""
        return self.acted and self.recovered and self.would_pay_anyway

    @property
    def futile(self) -> bool:
        """Never going to recover. Everything spent here was spent for nothing."""
        return self.acted and not self.recovered and not self.would_pay_anyway

    # --- buckets for transactions we ABSTAINED on --------------------------
    #
    # Restraint used to be scored as failure: leaving a customer alone who then
    # paid unaided landed in "wasted", and leaving a hopeless one alone landed in
    # "futile", as though we had spent money on them. Both are the system working
    # exactly as intended, at zero cost, and they are the project's central claim.

    @property
    def correct_restraint(self) -> bool:
        """Left alone, and they paid. The right call, for free."""
        return not self.acted and self.would_pay_anyway

    @property
    def correct_walkaway(self) -> bool:
        """Left alone, and nothing available would have recovered it either."""
        return not self.acted and not self.would_pay_anyway and not self.recoverable_within_caps

    @property
    def missed_opportunity(self) -> bool:
        """Left alone, but an affordable action would have worked. A real miss."""
        return not self.acted and not self.would_pay_anyway and self.recoverable_within_caps


@dataclass(frozen=True, slots=True)
class RunResult:
    """One policy over one split."""

    policy_name: str
    split: str
    outcomes: list[TxnOutcome]
    violations: list[ComplianceViolation] = field(default_factory=list)
    is_reference_bound: bool = False
    #: Wall-clock spent inside `decide()`. Reported to stdout only - it is
    #: machine-dependent, and writing it into artefacts would cost the
    #: byte-for-byte reproducibility the rest of the pipeline guarantees.
    decide_seconds_total: float = 0.0

    @property
    def n(self) -> int:
        return len(self.outcomes)

    @property
    def is_valid(self) -> bool:
        """False if the policy ever asked to exceed an invoice's attempt cap."""
        return not self.violations


def load_observed(path: Path) -> list[ObservedTransaction]:
    """Read `observed.jsonl` into the only shape a policy is allowed to see."""
    with path.open(encoding="utf-8") as fh:
        return [ObservedTransaction.from_row(json.loads(line)) for line in fh if line.strip()]


def load_counterfactuals(path: Path) -> dict[str, dict[str, Any]]:
    """Read the counterfactual store, keyed by transaction id.

    Only the harness calls this. A policy that needs it has already broken the
    isolation rule.
    """
    store: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            store[row["txn_id"]] = row
    return store


def filter_split(
    transactions: Sequence[ObservedTransaction], splits: Mapping[str, Any], split: str
) -> list[ObservedTransaction]:
    """Keep only transactions whose customer belongs to `split` ("all" keeps everything)."""
    if split == "all":
        return list(transactions)
    if split not in splits:
        raise KeyError(f"unknown split {split!r}; available: {sorted(k for k in splits if isinstance(splits[k], list))}")
    members = set(splits[split])
    return [t for t in transactions if t.customer_id in members]


def run(
    policy: Policy,
    transactions: Iterable[ObservedTransaction],
    oracle: Mapping[str, Mapping[str, Any]],
    split: str = "holdout",
) -> RunResult:
    """Run `policy` over `transactions` and score it against `oracle`.

    `transactions` are expected to be pre-filtered to `split` (see
    `filter_split`); `split` is recorded on the result as the label the report
    prints, so a scoreboard can never misreport which population it measured.
    """
    outcomes: list[TxnOutcome] = []
    violations: list[ComplianceViolation] = []
    decide_total = 0.0

    for txn in transactions:
        record = oracle[txn.txn_id]
        action_outcomes = record["outcomes"]
        would_pay_anyway = bool(record["would_pay_anyway"])

        started = time.perf_counter()
        decision, plan = validated_decision(policy, txn)
        decide_seconds = time.perf_counter() - started
        decide_total += decide_seconds

        # --- compliance gate ------------------------------------------------
        budget = txn.attempts_left
        requested = sum(action_cost(a).attempts_consumed for a in plan)
        breached = requested > budget

        # --- execute ---------------------------------------------------------
        attempts_used = 0
        cost_paise = 0
        annoyance = 0.0
        contacted = False
        executed: list[str] = []
        recovered = False
        hours_to_recovery: float | None = None

        for action in plan:
            cost = action_cost(action)
            if attempts_used + cost.attempts_consumed > budget:
                # Over the cap. Skip this action but keep walking: actions that
                # consume no debit attempt (re-consent, human handling) stay
                # legal even once the debit budget is gone, and dropping them
                # would understate what a capped invoice can still do.
                continue
            executed.append(action)
            attempts_used += cost.attempts_consumed
            cost_paise += cost.paise
            contacted = contacted or cost.contacts_customer
            annoyance += float(action_outcomes[action]["customer_annoyance_delta"])
            if action_outcomes[action]["recovered"]:
                recovered = True
                # Measured from the original failure, not from this action, so
                # a plan's earlier steps are already accounted for.
                hours_to_recovery = action_outcomes[action]["hours_to_recovery"]
                break  # paid; everything later in the plan is moot

        if not executed:
            # Nothing actually happened to this customer - either the policy
            # abstained, or the compliance gate truncated its entire plan. Both
            # leave the world exactly as it was, so their own behaviour decides
            # the outcome. Scoring a truncated plan as a lost recovery would
            # invent a cannibalisation that never physically occurred.
            recovered = would_pay_anyway
            annoyance = float(action_outcomes[_NULL_OUTCOME]["customer_annoyance_delta"])
            hours_to_recovery = action_outcomes[_NULL_OUTCOME]["hours_to_recovery"]

        recovering = [
            a for a in ACTIONS if a != _NULL_OUTCOME and action_outcomes[a]["recovered"]
        ]

        if breached:
            violations.append(
                ComplianceViolation(
                    txn_id=txn.txn_id,
                    policy_name=policy.name,
                    attempts_requested=requested,
                    attempts_allowed=budget,
                    plan=plan,
                    executed=tuple(executed),
                )
            )

        outcomes.append(
            TxnOutcome(
                txn_id=txn.txn_id,
                customer_id=txn.customer_id,
                amount_paise=txn.amount_paise,
                failure_code=txn.failure_code,
                decline_type=txn.decline_type,
                recovered=recovered,
                would_pay_anyway=would_pay_anyway,
                attempts_used=attempts_used,
                plan=plan,
                actions_executed=tuple(executed),
                total_cost_paise=cost_paise,
                annoyance_delta=annoyance,
                contacted_customer=contacted,
                compliance_violation=breached,
                reason=decision.reason,
                decide_seconds=decide_seconds,
                hours_to_recovery=hours_to_recovery,
                recoverable_within_caps=any(
                    ATTEMPTS_CONSUMED[a] <= budget for a in recovering
                ),
                recoverable_ignoring_caps=bool(recovering),
            )
        )

    return RunResult(
        policy_name=policy.name,
        split=split,
        outcomes=outcomes,
        violations=violations,
        is_reference_bound=bool(getattr(policy, "is_reference_bound", False)),
        decide_seconds_total=decide_total,
    )
