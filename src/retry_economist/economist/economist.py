"""The economist: turns a router's `Proposal` into an auditable go/no-go.

    EV(plan) = amount_paise * VALUE_CAPTURE_RATE * delta_p * discount(days)
               - action costs - annoyance cost
    delta_p  = p_recover_if_act - p_recover_if_abstain   (INCREMENTAL, never gross)

`delta_p` is incremental on purpose. `p_recover_if_act` alone answers "does
this plan often work"; it says nothing about whether the customer would have
paid anyway. Pricing the gross probability would make every plausible-looking
action look profitable, which is exactly the failure `llm_router_only` (no
economist) cannot avoid on its own - see Phase 4's `docs/PROGRESS.md`. Pricing
the INCREMENT prices only the difference the action itself is expected to make.

Two things happen in a fixed order, and the order is the entire point of this
module:

1. `compliance.apply_compliance` - five hard rules, none of which this module
   may override. A plan that fails a rule never reaches the arithmetic below;
   there is no expected value large enough to buy back a removed action.
2. THIS module's EV arithmetic - run only on whatever the compliance pass left
   standing.

`decide()` is the only public entry point, and it never proposes an action
that was not already in `proposal.proposed_plan`: compliance may only remove,
and a negative EV can only veto what remains, never substitute something else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Tuple

from retry_economist.economist.compliance import ComplianceResult, apply_compliance
from retry_economist.economist.costs import VALUE_CAPTURE_RATE, action_cost, annoyance_to_paise
from retry_economist.economist.estimator import Estimator
from retry_economist.economist.timing import (
    DAILY_DISCOUNT_RATE,
    discount,
    expected_days_for_plan,
    liquidity_days_from_signals,
)
from retry_economist.policies.base import ObservedTransaction

APPROVE = "approve"
APPROVE_TRUNCATED = "approve_truncated"
VETO = "veto"


@dataclass(frozen=True, slots=True)
class EVTerms:
    """Every number the EV formula touches, so the arithmetic is auditable
    line by line rather than trusted as a black box."""

    plan: Tuple[str, ...]
    amount_paise: int
    value_capture_rate: float
    p_recover_if_act: float
    p_recover_if_abstain: float
    delta_p: float
    expected_days_to_recovery: float
    daily_discount_rate: float
    discount_factor: float
    gross_value_paise: float
    action_cost_paise: int
    annoyance_units: float
    annoyance_cost_paise: float
    net_expected_value_paise: float
    estimator_label: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": list(self.plan),
            "amount_paise": self.amount_paise,
            "value_capture_rate": self.value_capture_rate,
            "p_recover_if_act": round(self.p_recover_if_act, 6),
            "p_recover_if_abstain": round(self.p_recover_if_abstain, 6),
            "delta_p": round(self.delta_p, 6),
            "expected_days_to_recovery": round(self.expected_days_to_recovery, 4),
            "daily_discount_rate": self.daily_discount_rate,
            "discount_factor": round(self.discount_factor, 6),
            "gross_value_paise": round(self.gross_value_paise, 2),
            "action_cost_paise": self.action_cost_paise,
            "annoyance_units": round(self.annoyance_units, 4),
            "annoyance_cost_paise": round(self.annoyance_cost_paise, 2),
            "net_expected_value_paise": round(self.net_expected_value_paise, 2),
            "estimator": self.estimator_label,
        }


def compute_ev(
    txn: ObservedTransaction,
    plan: Tuple[str, ...],
    proposal: Any,
    estimator: Estimator,
    *,
    daily_discount_rate: float = DAILY_DISCOUNT_RATE,
) -> EVTerms:
    """EV of executing exactly `plan` (already compliance-filtered) as-is.

    `plan` is a parameter distinct from `proposal.proposed_plan` deliberately:
    by the time this runs, compliance may have removed actions, and the EV
    priced here must be the EV of what will actually execute, not of what was
    originally asked for.

    `daily_discount_rate` is a parameter, not a hardcoded read of the module
    constant, so a sensitivity sweep can re-decide (not just re-price) at
    several rates - unlike CLV, the discount rate can change which plans clear
    the EV bar, so sweeping it means re-running decisions, not re-weighting
    already-executed ones.
    """
    p_act, p_abstain = estimator.estimate(txn, proposal)
    delta_p = p_act - p_abstain

    liquidity_days = liquidity_days_from_signals(getattr(proposal, "signals", None))
    days = expected_days_for_plan(plan, liquidity_days_until_credit=liquidity_days)
    factor = discount(days, daily_rate=daily_discount_rate)

    gross = txn.amount_paise * VALUE_CAPTURE_RATE * delta_p * factor

    action_cost_paise = sum(action_cost(a).paise for a in plan)
    annoyance_units = sum(action_cost(a).annoyance_units for a in plan)
    annoyance_cost = annoyance_to_paise(annoyance_units)

    net = gross - action_cost_paise - annoyance_cost

    return EVTerms(
        plan=tuple(plan),
        amount_paise=txn.amount_paise,
        value_capture_rate=VALUE_CAPTURE_RATE,
        p_recover_if_act=p_act,
        p_recover_if_abstain=p_abstain,
        delta_p=delta_p,
        expected_days_to_recovery=days,
        daily_discount_rate=daily_discount_rate,
        discount_factor=factor,
        gross_value_paise=gross,
        action_cost_paise=action_cost_paise,
        annoyance_units=annoyance_units,
        annoyance_cost_paise=annoyance_cost,
        net_expected_value_paise=net,
        estimator_label=estimator.label,
    )


@dataclass(frozen=True, slots=True)
class EconomistDecision:
    """The economist's verdict on one proposal, in full.

    `plan` is what the economist actually approved - the empty tuple on a
    veto, a subsequence of the proposal's plan on a truncation, or the
    proposal's plan unchanged on a full approval. It is never a plan the
    router did not propose.
    """

    txn_id: str
    verdict: str  # APPROVE | APPROVE_TRUNCATED | VETO
    plan: Tuple[str, ...]
    reason: str
    compliance: ComplianceResult
    ev: EVTerms | None
    proposed_plan: Tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        return {
            "txn_id": self.txn_id,
            "verdict": self.verdict,
            "plan": list(self.plan),
            "proposed_plan": list(self.proposed_plan),
            "reason": self.reason,
            "compliance": self.compliance.to_dict(),
            "ev": None if self.ev is None else self.ev.to_dict(),
        }


class Economist:
    """Approve, truncate, or veto - never reroute.

    Takes its `Estimator` as a required constructor argument, with no default.
    Phase 4 found the router's own probabilities lose to a per-code historical
    prior on both `p_recover_if_act` and `p_recover_if_abstain` (see
    `docs/PROGRESS.md`); which estimator this economist should use by default
    is a conclusion for whoever wires it into the evaluation CLI to draw from
    that comparison on the run that actually happened; it is not this
    constructor's decision to make silently.
    """

    def __init__(self, estimator: Estimator, *, daily_discount_rate: float = DAILY_DISCOUNT_RATE) -> None:
        self.estimator = estimator
        #: Overridable so a sensitivity sweep can re-decide at several rates;
        #: see `compute_ev`'s docstring for why this cannot be a re-pricing
        #: like the CLV sweep is.
        self.daily_discount_rate = daily_discount_rate

    def decide(self, txn: ObservedTransaction, proposal: Any) -> EconomistDecision:
        original_plan: Tuple[str, ...] = tuple(getattr(proposal, "proposed_plan", ()) or ())

        if not original_plan:
            return EconomistDecision(
                txn_id=txn.txn_id,
                verdict=VETO,
                plan=(),
                reason="router proposed no action; nothing for the economist to price",
                compliance=apply_compliance(txn, ()),
                ev=None,
                proposed_plan=(),
            )

        compliance = apply_compliance(txn, original_plan)

        if not compliance.allowed_plan:
            fired = ", ".join(c.rule_id for c in compliance.fired) or "compliance"
            return EconomistDecision(
                txn_id=txn.txn_id,
                verdict=VETO,
                plan=(),
                reason=f"{fired}: every proposed action was vetoed before any expected-value arithmetic ran",
                compliance=compliance,
                ev=None,
                proposed_plan=original_plan,
            )

        ev = compute_ev(
            txn,
            compliance.allowed_plan,
            proposal,
            self.estimator,
            daily_discount_rate=self.daily_discount_rate,
        )

        if ev.net_expected_value_paise <= 0:
            return EconomistDecision(
                txn_id=txn.txn_id,
                verdict=VETO,
                plan=(),
                reason=(
                    f"net expected value {ev.net_expected_value_paise:.2f} paise <= 0 for "
                    f"{list(compliance.allowed_plan)} under the {ev.estimator_label} estimator"
                ),
                compliance=compliance,
                ev=ev,
                proposed_plan=original_plan,
            )

        verdict = APPROVE_TRUNCATED if compliance.is_truncated else APPROVE
        reason = (
            f"net expected value +{ev.net_expected_value_paise:.2f} paise for "
            f"{list(compliance.allowed_plan)} under the {ev.estimator_label} estimator"
        )
        if verdict == APPROVE_TRUNCATED:
            reason = f"approved a profitable prefix after compliance filtering - {reason}"

        return EconomistDecision(
            txn_id=txn.txn_id,
            verdict=verdict,
            plan=compliance.allowed_plan,
            reason=reason,
            compliance=compliance,
            ev=ev,
            proposed_plan=original_plan,
        )
