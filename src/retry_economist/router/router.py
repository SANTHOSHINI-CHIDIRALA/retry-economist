"""One LLM call per transaction, in, and one Proposal out.

THE ROUTER PROPOSES. IT CANNOT EXECUTE.

`Proposal` is a separate type from `Decision`, and that separation is enforced
rather than documented: the simulator only accepts a `Decision`, `Decision` is
never constructed anywhere in this package, and a test walks this module's
syntax tree to keep it that way. A model's suggestion has to pass through a
policy - and, from the next phase, an economist - before it can spend anything.

The prompt carries the transaction facts, the raw gateway message, the three
computed signals with their confidences, the remaining attempt budget and the
allowed actions. One call, no tools, no multi-turn: a recovery decision worth a
few rupees cannot justify an agent loop, and every extra turn is another chance
to drift away from the schema.

Degradation is one-directional. Unparseable output becomes an ABSTAIN proposal
and a counter, never a guessed plan. A malfunctioning model must cost nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Sequence

from retry_economist.llm.provider import ParseFailure
from retry_economist.policies.base import ObservedTransaction, PLANNABLE_ACTIONS
from retry_economist.router.signals import SignalIndex, Signals
from retry_economist.schema import ATTEMPTS_CONSUMED

#: The JSON contract the model is held to.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "root_cause": {"type": "string"},
        "root_cause_confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "issuer_assessment": {"type": "string"},
        "liquidity_assessment": {"type": "string"},
        "proposed_plan": {
            "type": "array",
            "items": {"type": "string", "enum": list(PLANNABLE_ACTIONS)},
        },
        "rationale": {"type": "string"},
        "p_recover_if_act": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "p_recover_if_abstain": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        # Gemini's structured-output schema is validated as a Pydantic model, not
        # raw JSON Schema - it has no union-type syntax. `nullable` is its
        # equivalent for "string or null".
        "draft_customer_message": {"type": "string", "nullable": True},
    },
    "required": [
        "root_cause",
        "root_cause_confidence",
        "issuer_assessment",
        "liquidity_assessment",
        "proposed_plan",
        "rationale",
        "p_recover_if_act",
        "p_recover_if_abstain",
    ],
}


@dataclass(frozen=True, slots=True)
class Proposal:
    """A suggestion. Deliberately NOT a `Decision`, and deliberately inert.

    It carries no `plan` attribute and no `reason` attribute, so it cannot be
    passed where a `Decision` is expected even by accident - the simulator's
    isinstance check rejects it, and the attribute names do not line up either.
    Turning a proposal into something executable is a policy's job, done
    explicitly and visibly.
    """

    txn_id: str
    root_cause: str
    root_cause_confidence: float
    issuer_assessment: str
    liquidity_assessment: str
    proposed_plan: tuple[str, ...]
    rationale: str
    p_recover_if_act: float
    p_recover_if_abstain: float
    draft_customer_message: str | None
    signals: Signals
    #: True when the model failed to produce usable output and this proposal is
    #: the safe fallback rather than a considered answer.
    parse_failed: bool = False

    @property
    def proposes_action(self) -> bool:
        return bool(self.proposed_plan)

    def to_dict(self) -> dict[str, Any]:
        return {
            "txn_id": self.txn_id,
            "root_cause": self.root_cause,
            "root_cause_confidence": round(self.root_cause_confidence, 4),
            "issuer_assessment": self.issuer_assessment,
            "liquidity_assessment": self.liquidity_assessment,
            "proposed_plan": list(self.proposed_plan),
            "rationale": self.rationale,
            "p_recover_if_act": round(self.p_recover_if_act, 4),
            "p_recover_if_abstain": round(self.p_recover_if_abstain, 4),
            "draft_customer_message": self.draft_customer_message,
            "parse_failed": self.parse_failed,
            "signals": self.signals.to_dict(),
        }


@dataclass
class RouterStats:
    proposals: int = 0
    parse_failures: int = 0
    abstain_proposals: int = 0
    schema_violations: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposals": self.proposals,
            "parse_failures": self.parse_failures,
            "abstain_proposals": self.abstain_proposals,
            "schema_violations": self.schema_violations,
        }


_SYSTEM = """\
You are a payment recovery analyst for an Indian payment gateway. A payment has
failed. Decide what, if anything, should be attempted to recover it.

You are advising, not acting. Your proposal is reviewed before anything runs.

Rules you must respect:
- Choose only from the allowed actions listed below. An empty plan means "do
  nothing", which is frequently the right answer: many customers pay unaided,
  and every action costs money and customer goodwill.
- Never propose more debit attempts than the remaining attempt budget allows.
- Hard declines (blocked, closed or fraud-flagged instruments) can never be
  cleared by retrying on any rail, at any hour.
- Your rationale must cite at least one of the three signals by name.

The two probabilities matter as much as the plan. Downstream economics compare
them directly, so estimate them honestly - a confident number that is wrong is
worse than an uncertain number that is calibrated.
"""


def build_prompt(txn: ObservedTransaction, signals: Signals) -> str:
    """Render the facts, signals and constraints into one prompt.

    The machine-readable block is delimited so the deterministic stand-in can
    read exactly what a model would read, and so a reviewer can diff the inputs
    of two runs without parsing prose.
    """
    facts = {
        "txn_id": txn.txn_id,
        "failure_code": txn.failure_code,
        "gateway_message": txn.gateway_message,
        "decline_type": txn.decline_type,
        "method": txn.method,
        "issuer": txn.issuer,
        "is_recurring": txn.is_recurring,
        "amount_rupees": round(txn.amount_rupees, 2),
        "attempts_used": txn.retry_attempts_used,
        "attempts_left": txn.attempts_left,
        "retry_cap": txn.retry_cap,
        "mandate_id": txn.mandate_id,
        "mandate_expiry": None if txn.mandate_expiry is None else txn.mandate_expiry.isoformat(),
        "created_at": txn.created_at.isoformat(),
        "tenure_days": txn.tenure_days,
        "past_txn_count": txn.past_txn_count,
        "past_success_rate": txn.past_success_rate,
        "prior_failed_attempts_this_invoice": txn.prior_failed_attempts_this_invoice,
        "comms_received_last_7d": txn.comms_received_last_7d,
        "preferred_method": txn.preferred_method,
        "city_tier": txn.city_tier,
        "signals": signals.to_dict(),
        "allowed_actions": list(PLANNABLE_ACTIONS),
        "attempts_consumed_by_action": {a: ATTEMPTS_CONSUMED[a] for a in PLANNABLE_ACTIONS},
    }
    return (
        _SYSTEM
        + "\n<FACTS>\n"
        + json.dumps(facts, indent=2, sort_keys=True)
        + "\n</FACTS>\n\n"
        + "Return one JSON object matching the required schema.\n"
    )


class Router:
    """Turns one observed transaction into one Proposal."""

    def __init__(self, provider: Any, index: SignalIndex) -> None:
        self.provider = provider
        self.index = index
        self.stats = RouterStats()

    def propose(self, txn: ObservedTransaction) -> Proposal:
        signals = self.index.signals_for(txn)
        prompt = build_prompt(txn, signals)
        self.stats.proposals += 1

        try:
            raw = self.provider.complete(prompt, RESPONSE_SCHEMA)
        except ParseFailure:
            self.stats.parse_failures += 1
            self.stats.abstain_proposals += 1
            return self._abstain(txn, signals, parse_failed=True)

        proposal = self._coerce(txn, signals, raw)
        if not proposal.proposes_action:
            self.stats.abstain_proposals += 1
        return proposal

    def _coerce(
        self, txn: ObservedTransaction, signals: Signals, raw: dict[str, Any]
    ) -> Proposal:
        """Validate the response, degrading to abstention rather than guessing.

        Structured-output mode is a request, not a guarantee. Anything outside
        the contract - an unknown action, a probability outside [0, 1] - is
        treated as a schema violation, and the safe answer is to do nothing.
        """
        plan_raw = raw.get("proposed_plan") or []
        if not isinstance(plan_raw, list):
            self.stats.schema_violations += 1
            self.stats.abstain_proposals += 1
            return self._abstain(txn, signals, parse_failed=True)

        plan: list[str] = []
        for action in plan_raw:
            if action in PLANNABLE_ACTIONS:
                plan.append(action)
            else:
                # An action outside the allowed set is not a near-miss to be
                # repaired; it means the response cannot be trusted as a whole.
                self.stats.schema_violations += 1
                self.stats.abstain_proposals += 1
                return self._abstain(txn, signals, parse_failed=True)

        try:
            p_act = _unit(raw.get("p_recover_if_act"))
            p_abstain = _unit(raw.get("p_recover_if_abstain"))
            confidence = _unit(raw.get("root_cause_confidence"))
        except (TypeError, ValueError):
            self.stats.schema_violations += 1
            self.stats.abstain_proposals += 1
            return self._abstain(txn, signals, parse_failed=True)

        return Proposal(
            txn_id=txn.txn_id,
            root_cause=str(raw.get("root_cause", signals.root_cause.value)),
            root_cause_confidence=confidence,
            issuer_assessment=str(raw.get("issuer_assessment", "")),
            liquidity_assessment=str(raw.get("liquidity_assessment", "")),
            proposed_plan=tuple(plan),
            rationale=str(raw.get("rationale", "")).strip() or "(no rationale returned)",
            p_recover_if_act=p_act,
            p_recover_if_abstain=p_abstain,
            draft_customer_message=(
                None
                if raw.get("draft_customer_message") in (None, "")
                else str(raw["draft_customer_message"])
            ),
            signals=signals,
        )

    def _abstain(
        self, txn: ObservedTransaction, signals: Signals, *, parse_failed: bool
    ) -> Proposal:
        """The safe fallback. Costs nothing and cannot damage a customer."""
        return Proposal(
            txn_id=txn.txn_id,
            root_cause=signals.root_cause.value,
            root_cause_confidence=signals.root_cause.confidence,
            issuer_assessment=signals.issuer_health_now.summary,
            liquidity_assessment=signals.liquidity_timing.summary,
            proposed_plan=(),
            rationale=(
                "model output could not be used; abstaining rather than spending on a guess"
                if parse_failed
                else "no action proposed"
            ),
            # An abstain fallback must not claim knowledge it does not have.
            p_recover_if_act=0.0,
            p_recover_if_abstain=0.0,
            draft_customer_message=None,
            signals=signals,
            parse_failed=parse_failed,
        )

    def propose_many(self, transactions: Sequence[ObservedTransaction]) -> list[Proposal]:
        return [self.propose(txn) for txn in transactions]


def _unit(value: Any) -> float:
    """Coerce to a probability, rejecting anything outside [0, 1]."""
    number = float(value)
    if not (0.0 <= number <= 1.0):
        raise ValueError(f"probability out of range: {number}")
    return number
