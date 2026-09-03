"""THE CHEATING UPPER BOUND. NOT A RESULT. NOT A POLICY ANYONE COULD DEPLOY.

This is the single allow-listed exception to the isolation rule in `base.py`.
It reads the counterfactual outcome store and picks the action already known to
work, which is exactly what a real policy cannot do: at decision time nobody
knows which action would have succeeded.

It earns its place by answering "how much was ever available to win?". Without
it, a scoreboard says a policy recovered 40% and leaves the reader guessing
whether the remaining 60% was reachable at all.

It only reaches for actions the invoice's remaining debit budget can pay for,
so it bounds what a COMPLIANT system could have achieved. The larger, cap-free
ceiling is a property of the dataset rather than of any policy - the harness
computes it analytically and reports it in the header - because a policy that
spends attempts the mandate forbids has its plan truncated by the compliance
gate and therefore measures neither bound.

Three guardrails keep it from being mistaken for a result:

- it is named "oracle_best (CHEATS)", and that name is what the reports print;
- it takes its data source as an explicit constructor argument, so it can never
  quietly acquire one by opening a file the way a leaking policy would;
- `is_reference_bound` is True, and the scoreboard renders such policies in a
  separate section, below the honest results.
"""

from __future__ import annotations

from typing import Any, Mapping

from retry_economist.policies.base import Decision, ObservedTransaction
from retry_economist.schema import ACTIONS, ATTEMPTS_CONSUMED


class OracleBestPolicy:
    """Picks the cheapest action already known to recover this payment.

    `ACTIONS` runs from least to most invasive, so taking the first success in
    that order is the least-cost winning play, not merely a winning one - the
    bound stays honest about spend as well as recovery.
    """

    is_reference_bound = True

    name = "oracle_best (CHEATS)"

    def __init__(self, outcomes_by_txn: Mapping[str, Mapping[str, Any]]) -> None:
        """
        Args:
            outcomes_by_txn: txn_id -> {action -> {"recovered": bool, ...}}.
                Passed in deliberately; nothing here goes looking for it.
        """
        self._outcomes = outcomes_by_txn

    def decide(self, txn: ObservedTransaction) -> Decision:
        outcomes = self._outcomes.get(txn.txn_id)
        if outcomes is None:
            return Decision(plan=[], reason="no counterfactual record for this transaction")

        budget = txn.attempts_left
        for action in ACTIONS:
            record = outcomes.get(action)
            if record is None or not record.get("recovered"):
                continue
            if ATTEMPTS_CONSUMED[action] > budget:
                # Known to work, but the mandate has no debit attempts left to
                # pay for it. A bound that spends attempts it does not have is
                # not a bound on anything achievable.
                continue
            if action == "do_nothing":
                return Decision(
                    plan=[],
                    reason="would have recovered unaided; acting could only add cost",
                    metadata={"known_best_action": action},
                )
            return Decision(
                plan=[action],
                reason=f"known to recover under {action}",
                metadata={"known_best_action": action},
            )

        return Decision(
            plan=[],
            reason="nothing recovers this payment; spending on it is pure loss",
            metadata={"known_best_action": None},
        )
