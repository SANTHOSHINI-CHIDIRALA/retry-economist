"""A hand-written expert system: no model, no arithmetic, just rules.

This baseline exists to answer the question any reviewer will ask first - would
a lookup table have done the same job? So it is built to win, not to lose. It
encodes what a competent payments engineer already knows from the acquirer's
decline-code documentation, applies it to every failure mode rather than one,
and includes the defensive checks such a system would grow within a month of
being live:

- an authoritative hard-decline check that does not depend on recognising the
  code, because a blocked instrument must never be retried;
- an expiry check on the mandate itself, since the date is right there in the
  record and a lapsed mandate cannot be debited whatever the code says;
- a keyword fallback over the raw gateway message for codes the table has never
  seen, because acquirer feeds add codes without notice;
- an attempt-budget guard, so the system never plans a debit the scheme rules
  forbid.

Every branch names the rule that fired. If a later phase cannot beat this, the
honest conclusion is that the failure code is sufficient and the extra machinery
is not earning its place - and this file is what makes that conclusion checkable
rather than rhetorical.
"""

from __future__ import annotations

from dataclasses import dataclass

from retry_economist.policies.base import Decision, ObservedTransaction
from retry_economist.schema import ATTEMPTS_CONSUMED

#: Sentinel for a rule that deliberately does nothing.
ABSTAIN: str | None = None


@dataclass(frozen=True, slots=True)
class Rule:
    """One decision, and the reasoning a reviewer would want to see."""

    rule_id: str
    action: str | None
    because: str

    def reason(self) -> str:
        target = self.action or "abstain"
        return f"{self.rule_id}: {target} - {self.because}"


#: The decline-code table. Keyed on the code the acquirer actually sends, since
#: that is what arrives in production; the failure taxonomy behind it is not
#: visible at decision time.
RULES: dict[str, Rule] = {
    "41": Rule(
        "R-HARD-41",
        ABSTAIN,
        "card blocked, lost or the account is closed; no retry on any rail can clear it",
    ),
    "R05": Rule(
        "R-HARD-R05",
        ABSTAIN,
        "declined by the risk engine; retrying re-triggers the same rule and risks the BIN",
    ),
    "MANDATE_EXPIRED_M06": Rule(
        "R-MANDATE",
        "request_new_mandate",
        "consent has lapsed, so the debit is rejected before it reaches the account; "
        "only fresh authorisation restores the rail",
    ),
    "91": Rule(
        "R-DOWNTIME",
        "retry_in_24h",
        "issuer is inoperative; outages here resolve in hours, so wait out the incident "
        "rather than burning attempts into a dead endpoint",
    ),
    "51": Rule(
        "R-FUNDS",
        "retry_next_salary_day",
        "account is dry; balance follows the salary cycle, so time the retry to the "
        "credit instead of retrying into the same empty account",
    ),
    "U69": Rule(
        "R-COLLECT",
        "switch_to_upi_intent",
        "payer never answered the collect request; a push request they tap converts far "
        "better than re-sending a pull they already ignored",
    ),
    "ACS_TIMEOUT": Rule(
        "R-3DS",
        "nudge_then_retry",
        "cardholder abandoned the OTP page; a reminder recovers intent before re-presenting",
    ),
    "96": Rule(
        "R-TECH",
        "retry_in_2h",
        "gateway malfunction; transient, so a short wait clears it",
    ),
}

#: Fallback for codes the table has not seen. Ordered: the first keyword found
#: in the raw gateway message wins. Deliberately tolerant of the messy casing
#: and abbreviations real acquirer feeds use.
_MESSAGE_FALLBACKS: tuple[tuple[tuple[str, ...], Rule], ...] = (
    (
        ("insufficient", "low bal", "not sufficient", "bal too low", "no funds"),
        Rule("R-FALLBACK-FUNDS", "retry_next_salary_day", "message reads as a balance failure"),
    ),
    (
        ("not responding", "unavailable", "inoperative", "timeout at", "not available"),
        Rule("R-FALLBACK-DOWN", "retry_in_24h", "message reads as an issuer outage"),
    ),
    (
        ("mandate", "umrn", "e-mandate", "re-registration"),
        Rule("R-FALLBACK-MANDATE", "request_new_mandate", "message reads as lapsed consent"),
    ),
    (
        ("otp", "3ds", "authentication", "acs"),
        Rule("R-FALLBACK-3DS", "nudge_then_retry", "message reads as an abandoned authentication"),
    ),
    (
        ("collect", "did not respond", "expired"),
        Rule("R-FALLBACK-COLLECT", "switch_to_upi_intent", "message reads as an ignored collect"),
    ),
    (
        ("blocked", "do not honour", "pick up card", "closed", "fraud", "risk"),
        Rule("R-FALLBACK-HARD", ABSTAIN, "message reads as a permanent block"),
    ),
)

#: Last resort for a code and a message that match nothing at all. A single
#: short-delay retry is the cheapest safe guess; it is one attempt, not three.
_DEFAULT = Rule(
    "R-DEFAULT",
    "retry_in_2h",
    "unrecognised decline; one short-delay retry is the cheapest safe guess",
)


class RulesOnlyPolicy:
    """Expert system over the failure code and the observable record."""

    name = "rules_only"

    def decide(self, txn: ObservedTransaction) -> Decision:
        rule = self._select(txn)

        # Budget guard. A rule that wants a debit the invoice cannot pay for is
        # overruled here rather than left for the compliance gate to truncate:
        # planning an attempt that cannot legally run is a defect, not a style.
        if rule.action is not None and ATTEMPTS_CONSUMED[rule.action] > txn.attempts_left:
            blocked = Rule(
                f"{rule.rule_id}/BUDGET",
                ABSTAIN,
                f"would have chosen {rule.action} but the mandate has "
                f"{txn.attempts_left} debit attempts left",
            )
            return self._decision(blocked, txn)

        return self._decision(rule, txn)

    def _select(self, txn: ObservedTransaction) -> Rule:
        # 1. Hard declines, decided on the authoritative field rather than on
        #    recognising the code. Nothing below may override this.
        if txn.decline_type == "hard":
            return RULES.get(
                txn.failure_code,
                Rule(
                    "R-HARD-ANY",
                    ABSTAIN,
                    f"acquirer classed {txn.failure_code} as a hard decline; "
                    "the instrument is dead and no retry can revive it",
                ),
            )

        # 2. A mandate that has already lapsed cannot be debited, whatever the
        #    code says. The expiry date is in the record; use it.
        if (
            txn.mandate_id is not None
            and txn.mandate_expiry is not None
            and txn.mandate_expiry <= txn.created_at
        ):
            return Rule(
                "R-MANDATE-EXPIRED-DATE",
                "request_new_mandate",
                f"mandate {txn.mandate_id} expired on "
                f"{txn.mandate_expiry.date()}, before this attempt",
            )

        # 3. The code table: the ordinary path.
        if txn.failure_code in RULES:
            return RULES[txn.failure_code]

        # 4. Unknown code - read the message the acquirer actually sent.
        message = txn.gateway_message.lower()
        for keywords, rule in _MESSAGE_FALLBACKS:
            if any(keyword in message for keyword in keywords):
                return rule

        return _DEFAULT

    def _decision(self, rule: Rule, txn: ObservedTransaction) -> Decision:
        return Decision(
            plan=[] if rule.action is None else [rule.action],
            reason=rule.reason(),
            metadata={
                "rule_id": rule.rule_id,
                "failure_code": txn.failure_code,
                "attempts_left": txn.attempts_left,
            },
        )
