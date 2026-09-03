"""The abstain baseline: never intervene.

This is not a strawman. A meaningful share of failed payments recover on their
own, at zero cost and zero customer irritation, so abstaining is a genuinely
strong policy on both spend and goodwill. Every other policy has to earn its
attempts against this line, and any policy that cannot beat it is worse than
switching the recovery system off.
"""

from __future__ import annotations

from retry_economist.policies.base import Decision, ObservedTransaction


class DoNothingPolicy:
    """Always abstains."""

    name = "do_nothing"

    def decide(self, txn: ObservedTransaction) -> Decision:
        return Decision(plan=[], reason="no action")
