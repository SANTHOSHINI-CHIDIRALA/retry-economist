"""What each action costs, in one place.

Every constant here is an ESTIMATE, chosen to be defensible rather than exact,
and each carries the real-world basis it came from. They live in a single module
because the conclusion of this whole project - "which action is worth taking" -
is a function of these numbers, and a reader who disagrees with the ranking
should be able to find every assumption behind it in one screen and re-run with
their own.

All money is in paise. Integers throughout: rupee floats accumulate error over
hundreds of thousands of transactions, and money that does not add up exactly is
money nobody trusts.
"""

from __future__ import annotations

from dataclasses import dataclass

from retry_economist.schema import ACTIONS, ATTEMPTS_CONSUMED

# --- direct, invoiced costs -------------------------------------------------

#: ESTIMATE. Per debit attempt: gateway transaction fee plus the bank's
#: authorisation charge. Small individually, which is exactly why blind retry
#: schedules get waved through - the damage only shows up at volume.
ATTEMPT_COST_PAISE = 200

#: ESTIMATE. One transactional SMS at Indian bulk-messaging rates.
SMS_COST_PAISE = 20

#: ESTIMATE. One WhatsApp business-initiated utility conversation.
WHATSAPP_COST_PAISE = 30

#: ESTIMATE. Generating and hosting a hosted payment link for one invoice.
PAYMENT_LINK_COST_PAISE = 30

#: ESTIMATE. Re-collecting mandate consent: the registration flow itself plus
#: the support load it reliably generates, since customers ring up to ask what
#: the re-authorisation request is for.
NEW_MANDATE_REQUEST_PAISE = 500

#: ESTIMATE. One human agent handling one case, at roughly a quarter-hour of
#: loaded support cost. Two orders of magnitude above a retry, which is the
#: single most important ratio in the model: it is what stops "escalate
#: everything" from being the right answer.
HUMAN_ESCALATION_PAISE = 4500

# --- indirect costs ---------------------------------------------------------

# Annoyance is priced through the customer relationship it damages, not as a
# flat fee. A flat fee made every action unconditionally profitable - a retry
# cost two rupees and could recover hundreds - which would leave nothing for an
# economist layer to decide. Chasing an invoice is a bet against the customer's
# remaining lifetime value, and that is the quantity the bet has to be scored in.
#
#     annoyance_cost = annoyance_delta * ANNOYANCE_TO_CHURN_PER_UNIT * CLV
#
#: ASSUMPTION, NOT A MEASUREMENT. Nobody here has measured lifetime value for
#: this population; INR 12,000 is a plausible figure for a mid-market Indian
#: subscription or lending customer and no more than that. It is the single most
#: load-bearing guess in the model, which is why the CLI ships `--clv-sweep`:
#: any conclusion that changes when this number changes is a conclusion about
#: the guess rather than about the policy, and the report says which it is.
CUSTOMER_LIFETIME_VALUE_PAISE = 1_200_000  # ~ INR 12,000

#: ASSUMPTION, NOT A MEASUREMENT. Added probability of churn per unit of
#: annoyance. At 0.08, eight units of accumulated irritation would be expected
#: to lose the customer outright.
ANNOYANCE_TO_CHURN_PER_UNIT = 0.08

#: Share of a recovered invoice the merchant actually keeps. 1.0 models a
#: merchant who recovers the full amount; lower it to model revenue share.
VALUE_CAPTURE_RATE = 1.0


@dataclass(frozen=True, slots=True)
class ActionCost:
    """The full price of taking one action once."""

    paise: int
    attempts_consumed: int
    contacts_customer: bool


#: Cost components per action. `attempts_consumed` is imported from the schema
#: rather than restated, so the compliance gate and the cost model can never
#: drift apart on what counts as a debit attempt.
_ACTION_COSTS: dict[str, ActionCost] = {
    "do_nothing": ActionCost(
        paise=0,
        attempts_consumed=ATTEMPTS_CONSUMED["do_nothing"],
        contacts_customer=False,
    ),
    "retry_now": ActionCost(
        paise=ATTEMPT_COST_PAISE,
        attempts_consumed=ATTEMPTS_CONSUMED["retry_now"],
        contacts_customer=False,
    ),
    "retry_in_2h": ActionCost(
        paise=ATTEMPT_COST_PAISE,
        attempts_consumed=ATTEMPTS_CONSUMED["retry_in_2h"],
        contacts_customer=False,
    ),
    "retry_in_24h": ActionCost(
        paise=ATTEMPT_COST_PAISE,
        attempts_consumed=ATTEMPTS_CONSUMED["retry_in_24h"],
        contacts_customer=False,
    ),
    "retry_next_salary_day": ActionCost(
        paise=ATTEMPT_COST_PAISE,
        attempts_consumed=ATTEMPTS_CONSUMED["retry_next_salary_day"],
        contacts_customer=False,
    ),
    # A reminder plus a link plus the debit behind it: three line items, which
    # is why "just nudge them" is never as cheap as it sounds.
    "nudge_then_retry": ActionCost(
        paise=WHATSAPP_COST_PAISE + PAYMENT_LINK_COST_PAISE + ATTEMPT_COST_PAISE,
        attempts_consumed=ATTEMPTS_CONSUMED["nudge_then_retry"],
        contacts_customer=True,
    ),
    "switch_to_upi_intent": ActionCost(
        paise=SMS_COST_PAISE + ATTEMPT_COST_PAISE,
        attempts_consumed=ATTEMPTS_CONSUMED["switch_to_upi_intent"],
        contacts_customer=True,
    ),
    "request_new_mandate": ActionCost(
        paise=NEW_MANDATE_REQUEST_PAISE + SMS_COST_PAISE,
        attempts_consumed=ATTEMPTS_CONSUMED["request_new_mandate"],
        contacts_customer=True,
    ),
    "escalate_to_human": ActionCost(
        paise=HUMAN_ESCALATION_PAISE,
        attempts_consumed=ATTEMPTS_CONSUMED["escalate_to_human"],
        contacts_customer=True,
    ),
}

assert set(_ACTION_COSTS) == set(ACTIONS), "cost table and action space disagree"


def action_cost(action: str) -> ActionCost:
    """Cost of one action. Raises `KeyError` on an unknown action name."""
    try:
        return _ACTION_COSTS[action]
    except KeyError:
        raise KeyError(f"no cost defined for action {action!r}; known: {list(ACTIONS)}") from None


def annoyance_to_paise(
    annoyance_units: float, *, clv_paise: int = CUSTOMER_LIFETIME_VALUE_PAISE
) -> int:
    """Expected lifetime value destroyed by `annoyance_units` of irritation.

    `clv_paise` is a parameter rather than a constant lookup so the sensitivity
    sweep can re-price a completed run without re-simulating it.
    """
    return round(annoyance_units * ANNOYANCE_TO_CHURN_PER_UNIT * clv_paise)


def annoyance_paise_per_unit(clv_paise: int = CUSTOMER_LIFETIME_VALUE_PAISE) -> float:
    """Cost of one annoyance unit at a given lifetime value."""
    return ANNOYANCE_TO_CHURN_PER_UNIT * clv_paise


def recovered_value_paise(amount_paise: int) -> int:
    """What the merchant actually keeps from recovering `amount_paise`."""
    return round(amount_paise * VALUE_CAPTURE_RATE)


def cost_constants() -> dict[str, float]:
    """Every constant, for the report header - assumptions travel with results."""
    return {
        "ATTEMPT_COST_PAISE": ATTEMPT_COST_PAISE,
        "SMS_COST_PAISE": SMS_COST_PAISE,
        "WHATSAPP_COST_PAISE": WHATSAPP_COST_PAISE,
        "PAYMENT_LINK_COST_PAISE": PAYMENT_LINK_COST_PAISE,
        "NEW_MANDATE_REQUEST_PAISE": NEW_MANDATE_REQUEST_PAISE,
        "HUMAN_ESCALATION_PAISE": HUMAN_ESCALATION_PAISE,
        "CUSTOMER_LIFETIME_VALUE_PAISE": CUSTOMER_LIFETIME_VALUE_PAISE,
        "ANNOYANCE_TO_CHURN_PER_UNIT": ANNOYANCE_TO_CHURN_PER_UNIT,
        "ANNOYANCE_PAISE_PER_UNIT_DERIVED": annoyance_paise_per_unit(),
        "VALUE_CAPTURE_RATE": VALUE_CAPTURE_RATE,
    }
