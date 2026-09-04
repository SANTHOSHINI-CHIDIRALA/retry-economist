"""The ablation: adopt the router's proposal verbatim, with no economics at all.

This policy exists to isolate one number - what does the router contribute on
its own? - and it is EXPECTED to score badly on precision. It has no expected
value arithmetic, no veto, no cost model and no restraint beyond whatever the
router happened to propose. The only thing standing between it and a merchant's
money is the simulator's compliance gate.

That is the point. If the full system later beats this, the difference is
attributable to the economist layer rather than to the model; if it does not,
that is worth knowing too. Tuning this policy to look respectable would destroy
the measurement it exists to provide, so it is deliberately left as-is.

It is also the only policy here that consumes a `Proposal`. The conversion from
proposal to decision happens right here, in three visible lines, rather than
anywhere inside the router - which is what keeps "the model cannot execute" true.
"""

from __future__ import annotations

from retry_economist.policies.base import Decision, ObservedTransaction
from retry_economist.router.router import Router


class LLMRouterOnlyPolicy:
    """Adopts `proposed_plan` unchanged."""

    name = "llm_router_only (NO ECONOMIST)"

    def __init__(self, router: Router) -> None:
        self.router = router
        #: Kept so the scoreboard can report calibration and provenance without
        #: re-running the model.
        self.proposals: dict[str, object] = {}

    def decide(self, txn: ObservedTransaction) -> Decision:
        proposal = self.router.propose(txn)
        self.proposals[txn.txn_id] = proposal
        return Decision(
            plan=list(proposal.proposed_plan),
            reason=proposal.rationale,
            metadata={
                "root_cause": proposal.root_cause,
                "root_cause_confidence": proposal.root_cause_confidence,
                "p_recover_if_act": proposal.p_recover_if_act,
                "p_recover_if_abstain": proposal.p_recover_if_abstain,
                "parse_failed": proposal.parse_failed,
            },
        )
