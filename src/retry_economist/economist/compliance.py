"""Five hard rules, checked before any arithmetic runs.

Every rule here can only REMOVE actions from a proposed plan; none can add or
reorder one. That keeps "the economist may not invent an action the router
did not propose" true by construction, the same way `Proposal` being a
distinct type from `Decision` keeps "the router cannot execute" true by
construction (see `router/router.py`).

No expected value, however large, overrides a rule that fires here. That is
enforced by ORDER: `apply_compliance` runs first and hands `economist.py`
whatever survives; a filtered-out action is never seen by the EV arithmetic at
all, so there is no number it could produce that would bring the action back.
`tests/test_economist.py` checks this directly with amounts large enough that
an EV-only guard would wave anything through.

A note on "truncated": C3 (the attempt cap) truncates a genuine prefix, in the
sense the word usually means - it walks the plan in order and stops admitting
actions once the debit budget is spent, exactly as `eval/simulator.py`'s own
compliance gate does. C1, C2, C4 and C5 are not positional at all - a hard
decline strips every debit action wherever it sits in the plan, not just a
leading run of them. `economist.py` calls anything compliance removed a
"truncation" regardless of which rule did the removing, because from the
caller's side the distinction is not the point: what matters is that the
result is always a subsequence of what was proposed, never a rewrite of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple

from retry_economist.economist.costs import action_cost
from retry_economist.policies.base import ObservedTransaction
from retry_economist.router.signals import RISK, root_cause_signal

#: ASSUMPTION. Customer-facing contacts tolerated inside a rolling week before
#: further outreach is presumed to do more relationship damage than the
#: invoice is worth chasing. `comms_received_last_7d` is generated on
#: {0: 30%, 1: 26%, 2: 18%, 3: 12%, 4: 8%, 5: 6%} weights (see
#: `generator/customers.py`), so 3 caps roughly the top quartile rather than
#: never firing or always firing.
CONTACT_CAP = 3


@dataclass(frozen=True, slots=True)
class ComplianceCheck:
    """One rule's verdict, kept whether or not it fired - a clean rule is
    part of the audit trail too."""

    rule_id: str
    fired: bool
    reason: str
    removed: Tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ComplianceResult:
    """What survived the five rules, and the full log of what each one did."""

    original_plan: Tuple[str, ...]
    allowed_plan: Tuple[str, ...]
    checks: Tuple[ComplianceCheck, ...] = field(default_factory=tuple)

    @property
    def fired(self) -> Tuple[ComplianceCheck, ...]:
        return tuple(c for c in self.checks if c.fired)

    @property
    def is_truncated(self) -> bool:
        """Whether compliance removed anything at all from the proposed plan."""
        return self.allowed_plan != self.original_plan

    def to_dict(self) -> dict:
        return {
            "original_plan": list(self.original_plan),
            "allowed_plan": list(self.allowed_plan),
            "is_truncated": self.is_truncated,
            "checks": [
                {
                    "rule_id": c.rule_id,
                    "fired": c.fired,
                    "reason": c.reason,
                    "removed": list(c.removed),
                }
                for c in self.checks
            ],
        }


def _debit_actions(plan: Tuple[str, ...]) -> Tuple[str, ...]:
    return tuple(a for a in plan if action_cost(a).attempts_consumed > 0)


def _contact_actions(plan: Tuple[str, ...]) -> Tuple[str, ...]:
    return tuple(a for a in plan if action_cost(a).contacts_customer)


def _c1_risk_declined(txn: ObservedTransaction, plan: Tuple[str, ...]) -> ComplianceCheck:
    """C1 - a risk-engine decline is vetoed outright, at any expected value.

    Computed from the same deterministic signal the router itself is handed
    (`router/signals.py::root_cause_signal`), never from a proposal's own
    self-reported `root_cause` - a rule a model could talk its way past by
    mis-stating its own diagnosis would not be a hard rule.
    """
    is_risk = root_cause_signal(txn).value == RISK
    if is_risk and plan:
        return ComplianceCheck(
            "C1_RISK_DECLINED",
            True,
            "risk-engine decline: every action is vetoed regardless of expected value",
            removed=plan,
        )
    return ComplianceCheck(
        "C1_RISK_DECLINED", False, "not a risk-engine decline" if not is_risk else "no action proposed"
    )


def _c2_hard_decline_no_debit(txn: ObservedTransaction, plan: Tuple[str, ...]) -> ComplianceCheck:
    """C2 - a hard decline can never be cleared by another debit attempt.

    `request_new_mandate` is exempt: it collects fresh consent rather than
    putting another authorisation on the wire, so it is not a retry at all.
    """
    if txn.decline_type != "hard":
        return ComplianceCheck("C2_HARD_DECLINE_NO_DEBIT", False, "not a hard decline")
    removed = _debit_actions(plan)
    if not removed:
        return ComplianceCheck(
            "C2_HARD_DECLINE_NO_DEBIT", False, "hard decline, but the plan proposes no debit attempt"
        )
    return ComplianceCheck(
        "C2_HARD_DECLINE_NO_DEBIT",
        True,
        "hard decline: no debit retry may run on any rail, at any hour",
        removed=removed,
    )


def _c3_attempt_cap(txn: ObservedTransaction, plan: Tuple[str, ...]) -> ComplianceCheck:
    """C3 - the double-guard. The router is told the remaining budget in its
    prompt; this re-checks it independently of whatever the model did with
    that instruction, the same way `eval/simulator.py`'s compliance gate never
    trusts a policy's own arithmetic either.
    """
    budget = txn.attempts_left
    used = 0
    removed: list[str] = []
    for action in plan:
        consumed = action_cost(action).attempts_consumed
        if used + consumed > budget:
            removed.append(action)
            continue
        used += consumed
    if not removed:
        return ComplianceCheck("C3_ATTEMPT_CAP", False, f"plan fits the {budget} remaining attempt(s)")
    return ComplianceCheck(
        "C3_ATTEMPT_CAP",
        True,
        f"only {budget} debit attempt(s) remain; dropped what does not fit",
        removed=tuple(removed),
    )


def _c4_expired_mandate(txn: ObservedTransaction, plan: Tuple[str, ...]) -> ComplianceCheck:
    """C4 - a mandate that lapsed before this attempt cannot be debited, and no
    plan may assume otherwise until fresh consent (`request_new_mandate`) is
    in hand.
    """
    expired = (
        txn.mandate_id is not None
        and txn.mandate_expiry is not None
        and txn.mandate_expiry <= txn.created_at
    )
    if not expired:
        return ComplianceCheck("C4_EXPIRED_MANDATE", False, "mandate is not expired")
    removed = _debit_actions(plan)
    if not removed:
        return ComplianceCheck(
            "C4_EXPIRED_MANDATE", False, "mandate expired, but the plan proposes no debit attempt"
        )
    return ComplianceCheck(
        "C4_EXPIRED_MANDATE",
        True,
        f"mandate {txn.mandate_id} expired on {txn.mandate_expiry.date()}: "
        "no debit before fresh consent",
        removed=removed,
    )


def _c5_contact_cap(txn: ObservedTransaction, plan: Tuple[str, ...]) -> ComplianceCheck:
    """C5 - stop reaching out to a customer already contacted this week."""
    if txn.comms_received_last_7d < CONTACT_CAP:
        return ComplianceCheck(
            "C5_CONTACT_CAP",
            False,
            f"{txn.comms_received_last_7d} contact(s) in the last 7 days, under the cap of {CONTACT_CAP}",
        )
    removed = _contact_actions(plan)
    if not removed:
        return ComplianceCheck(
            "C5_CONTACT_CAP",
            False,
            f"{txn.comms_received_last_7d} contact(s) in the last 7 days, but the plan contacts nobody",
        )
    return ComplianceCheck(
        "C5_CONTACT_CAP",
        True,
        f"{txn.comms_received_last_7d} contact(s) already in the last 7 days, at or over the "
        f"cap of {CONTACT_CAP}: no further customer-facing action",
        removed=removed,
    )


#: Run in this order. Each rule sees only what the previous one left standing,
#: so a plan that trips two rules is fully explained by the log rather than by
#: whichever ran last.
_RULES = (
    _c1_risk_declined,
    _c2_hard_decline_no_debit,
    _c3_attempt_cap,
    _c4_expired_mandate,
    _c5_contact_cap,
)


def apply_compliance(txn: ObservedTransaction, plan: Tuple[str, ...]) -> ComplianceResult:
    """Run C1-C5 in order, each filtering whatever the previous one left.

    Order-preserving throughout: a rule may drop actions from anywhere in the
    plan, but it never reorders what remains, so `allowed_plan` is always a
    subsequence of `original_plan`.
    """
    original = tuple(plan)
    current = original
    checks: list[ComplianceCheck] = []

    for rule in _RULES:
        check = rule(txn, current)
        checks.append(check)
        if check.removed:
            removed_set = list(check.removed)
            survivors = []
            for action in current:
                if action in removed_set:
                    removed_set.remove(action)  # drop one occurrence, not every occurrence
                    continue
                survivors.append(action)
            current = tuple(survivors)

    return ComplianceResult(original_plan=original, allowed_plan=current, checks=tuple(checks))
