"""The policy contract, and the isolation rule that makes the scoreboard mean
anything.

ARCHITECTURAL RULE - A POLICY SEES ONLY THE OBSERVED TRANSACTION.

`decide()` is handed one `ObservedTransaction` and nothing else. A policy may
not import the evaluation package, may not read counterfactual outcome data,
and may not open any file to go looking for it. The reason is blunt: the
counterfactual store contains the answer for every action, so a policy with any
path to it is not being measured, it is being asked to copy. A single leak
turns the entire scoreboard into a transcription test, and the leak would not
show up as a crash - it would show up as an excellent result.

The rule is enforced three ways, because documentation alone never held:

1. `ObservedTransaction.from_row` rejects any row carrying a key outside the
   published observed field list, so counterfactual rows cannot be passed in
   even by accident.
2. `decide()` takes no other argument, so there is nowhere to smuggle state.
3. `tests/test_eval.py` walks the syntax tree of every module under
   `policies/` and fails the build on a counterfactual import, identifier, or
   file path.

There is exactly one allow-listed exception, `policies/oracle_best.py`, which
cheats deliberately to establish an upper bound. It is named "(CHEATS)", it
takes its data source as an explicit constructor argument rather than reaching
for it, and the scoreboard prints it in a separate reference-bounds section so
it can never be read as a result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from retry_economist.schema import ACTIONS, OBSERVED_FIELDS

#: The action a policy expresses by returning an empty plan. It is never a
#: member of a plan; see `validate_plan`.
NULL_ACTION = "do_nothing"

#: Actions a plan may actually contain: everything except the null action.
PLANNABLE_ACTIONS: tuple[str, ...] = tuple(a for a in ACTIONS if a != NULL_ACTION)


class InvalidPlan(ValueError):
    """A policy returned a plan the executor refuses to run."""


@dataclass(frozen=True, slots=True)
class ObservedTransaction:
    """One failed payment, exactly as a payment gateway would have logged it.

    This is the complete input to a policy. Every field here is something a real
    PSP has on file at decision time; nothing about the customer's true intent,
    balance, or eventual behaviour appears, because in production none of it
    would.
    """

    txn_id: str
    customer_id: str
    created_at: datetime
    amount_paise: int
    method: str
    issuer: str
    is_recurring: bool
    mandate_id: str | None
    mandate_expiry: datetime | None
    retry_attempts_used: int
    retry_cap: int
    failure_code: str
    gateway_message: str
    decline_type: str
    issuer_health_at_failure: float
    tenure_days: int
    past_txn_count: int
    past_success_rate: float
    prior_failed_attempts_this_invoice: int
    comms_received_last_7d: int
    preferred_method: str
    city_tier: int

    @property
    def attempts_left(self) -> int:
        """Debit attempts still permitted by the mandate or scheme rules."""
        return max(0, self.retry_cap - self.retry_attempts_used)

    @property
    def amount_rupees(self) -> float:
        return self.amount_paise / 100.0

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ObservedTransaction:
        """Build from one `observed.jsonl` row, refusing anything richer.

        The strict key check is the first line of defence for the isolation
        rule: hand this a counterfactual record and it raises rather than
        quietly handing a policy the answers.
        """
        keys = set(row)
        expected = set(OBSERVED_FIELDS)
        if keys != expected:
            extra = sorted(keys - expected)
            missing = sorted(expected - keys)
            raise InvalidPlan(
                "row is not an observed transaction "
                f"(unexpected keys: {extra}; missing keys: {missing})"
            )
        return cls(
            txn_id=row["txn_id"],
            customer_id=row["customer_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            amount_paise=row["amount_paise"],
            method=row["method"],
            issuer=row["issuer"],
            is_recurring=row["is_recurring"],
            mandate_id=row["mandate_id"],
            mandate_expiry=(
                datetime.fromisoformat(row["mandate_expiry"])
                if row["mandate_expiry"] is not None
                else None
            ),
            retry_attempts_used=row["retry_attempts_used"],
            retry_cap=row["retry_cap"],
            failure_code=row["failure_code"],
            gateway_message=row["gateway_message"],
            decline_type=row["decline_type"],
            issuer_health_at_failure=row["issuer_health_at_failure"],
            tenure_days=row["tenure_days"],
            past_txn_count=row["past_txn_count"],
            past_success_rate=row["past_success_rate"],
            prior_failed_attempts_this_invoice=row["prior_failed_attempts_this_invoice"],
            comms_received_last_7d=row["comms_received_last_7d"],
            preferred_method=row["preferred_method"],
            city_tier=row["city_tier"],
        )


@dataclass(frozen=True, slots=True)
class Decision:
    """What a policy returns: an ordered plan, and why.

    `reason` is mandatory rather than optional. A recovery decision that cannot
    be explained is not reviewable by the merchant whose customers it acts on,
    and later phases surface this string directly in operator tooling.
    """

    plan: list[str]
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Policy(Protocol):
    """Anything that can turn one observed failure into a plan.

    Deliberately a Protocol rather than a base class: policies in later phases
    will be assembled from very different machinery (rules, learned models, an
    LLM behind a cache), and none of them should have to inherit from us to be
    scored.
    """

    name: str

    def decide(self, txn: ObservedTransaction) -> Decision: ...


def validate_plan(plan: Sequence[str], *, policy_name: str = "<policy>") -> tuple[str, ...]:
    """Check a plan and return it as an immutable tuple.

    Rejects the null action inside a plan outright. Doing nothing is expressed
    as the empty plan and only as the empty plan, so that "the policy declined
    to act" is one state in the data rather than two that have to be kept in
    agreement everywhere downstream.
    """
    if isinstance(plan, str) or not isinstance(plan, Sequence):
        raise InvalidPlan(f"{policy_name}: plan must be a sequence of action names, got {plan!r}")

    checked: list[str] = []
    for position, action in enumerate(plan):
        if not isinstance(action, str):
            raise InvalidPlan(f"{policy_name}: plan[{position}] is not a string: {action!r}")
        if action == NULL_ACTION:
            raise InvalidPlan(
                f"{policy_name}: {NULL_ACTION!r} may not appear in a plan "
                f"(position {position} of {len(plan)}); return an empty plan instead"
            )
        if action not in ACTIONS:
            raise InvalidPlan(
                f"{policy_name}: unknown action {action!r} at position {position}; "
                f"known actions are {list(ACTIONS)}"
            )
        checked.append(action)
    return tuple(checked)


def validated_decision(policy: Policy, txn: ObservedTransaction) -> tuple[Decision, tuple[str, ...]]:
    """Call a policy and validate what comes back.

    Returns the decision untouched alongside the validated plan, so the caller
    keeps the policy's own `reason` and `metadata` for the audit trail even
    though it executes the normalised tuple.
    """
    decision = policy.decide(txn)
    if not isinstance(decision, Decision):
        raise InvalidPlan(
            f"{getattr(policy, 'name', policy)!r}: decide() must return a Decision, "
            f"got {type(decision).__name__}"
        )
    if not decision.reason or not decision.reason.strip():
        raise InvalidPlan(f"{getattr(policy, 'name', policy)!r}: decision.reason is required")
    return decision, validate_plan(decision.plan, policy_name=getattr(policy, "name", "<policy>"))
