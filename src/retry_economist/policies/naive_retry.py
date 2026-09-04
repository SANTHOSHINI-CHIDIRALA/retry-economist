"""The incumbent: retry three times, on everything, and hope.

This is not a strawman. It is close to what a large number of production dunning
systems actually do - a fixed schedule fired at every failed payment, with no
reference to why the payment failed. It is cheap to build, it recovers a real
share of invoices, and it is the thing any new system has to beat before anyone
will consider deploying it.

It is also the policy that makes the case for the whole project, because of what
it does indiscriminately: it burns debit attempts on blocked cards and closed
accounts that no retry can ever clear, it re-attempts empty accounts hours
before payday instead of days after it, and it charges a fee and an annoyance
cost for every one of those. The scoreboard is built to price exactly that.

The plan is truncated to the invoice's remaining attempt budget, so this is a
compliant baseline rather than a broken one - beating a policy that cannot
legally run would prove nothing.
"""

from __future__ import annotations

from retry_economist.policies.base import Decision, ObservedTransaction

#: Fixed escalation ladder: immediately, after a short pause, after a day.
#: Chosen without reference to the failure - that is the point.
SCHEDULE: tuple[str, ...] = ("retry_now", "retry_in_2h", "retry_in_24h")


class NaiveRetry3xPolicy:
    """Retries every failed payment up to three times on a fixed schedule."""

    name = "naive_retry_3x"

    def decide(self, txn: ObservedTransaction) -> Decision:
        # Truncation keeps the policy compliant. When the budget is already
        # spent the plan is empty and the transaction is simply left alone -
        # not because the policy chose restraint, but because the scheme rules
        # made the choice for it.
        plan = list(SCHEDULE[: txn.attempts_left])
        return Decision(
            plan=plan,
            reason="retry three times",
            metadata={
                "schedule": list(SCHEDULE),
                "attempts_left": txn.attempts_left,
                "truncated": len(plan) < len(SCHEDULE),
            },
        )
