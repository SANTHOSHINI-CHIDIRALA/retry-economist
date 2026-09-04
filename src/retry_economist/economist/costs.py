"""What each action costs, at decision time - duplicated from `eval/costs.py`
by necessity, not by accident.

`tests/test_eval.py::GUARDED_PACKAGES` bans this package from importing
anything under `retry_economist.eval`, on purpose: that is the layer with a
path to the counterfactual store (`simulator.py`, `calibration.py`), and the
guard is a blunt, syntactic check that forbids the whole package rather than
trying to prove any one module inside it is safe. `eval/costs.py` itself never
touches counterfactual data, but the guard cannot know that from an import
statement alone, so the money figures a live decision needs are restated here
instead of imported.

`tests/test_economist.py::test_cost_tables_do_not_drift` reads both tables and
asserts every shared figure is byte-identical, so a change on one side that is
not mirrored on the other fails the build immediately rather than silently.

All money is in paise, as integers, for the same reason `eval/costs.py` gives:
rupee floats accumulate error over hundreds of thousands of transactions.
"""

from __future__ import annotations

from dataclasses import dataclass

from retry_economist.schema import ACTIONS, ATTEMPTS_CONSUMED

# --- direct, invoiced costs (mirrors eval/costs.py) -------------------------

ATTEMPT_COST_PAISE = 200
SMS_COST_PAISE = 20
WHATSAPP_COST_PAISE = 30
PAYMENT_LINK_COST_PAISE = 30
NEW_MANDATE_REQUEST_PAISE = 500
HUMAN_ESCALATION_PAISE = 4500

# --- indirect costs (mirrors eval/costs.py) ----------------------------------

#: ASSUMPTION, NOT A MEASUREMENT - same figure eval/costs.py uses, so a plan
#: priced by the economist and a plan scored by the harness agree on what a
#: customer relationship is worth.
CUSTOMER_LIFETIME_VALUE_PAISE = 1_200_000  # ~ INR 12,000

#: ASSUMPTION, NOT A MEASUREMENT. Mirrors eval/costs.py.
ANNOYANCE_TO_CHURN_PER_UNIT = 0.08

#: Share of a recovered invoice the merchant actually keeps. Mirrors eval/costs.py.
VALUE_CAPTURE_RATE = 1.0

#: ASSUMPTION, NOT A MEASUREMENT, and with NO counterpart in eval/costs.py.
#: `eval/costs.py` prices annoyance from the oracle's realised
#: `customer_annoyance_delta` - a number this package is structurally forbidden
#: to see. A decision made before anything runs cannot know the realised delta,
#: only an ex-ante estimate of it, so the economist needs its own per-action
#: units. Ordered the same way the generator's own reasoning goes: silence is
#: free, and a request for fresh consent asks the most of a customer.
EXPECTED_ANNOYANCE_UNITS: dict[str, float] = {
    "do_nothing": 0.00,
    "retry_now": 0.03,
    "retry_in_2h": 0.03,
    "retry_in_24h": 0.02,
    "retry_next_salary_day": 0.02,
    "nudge_then_retry": 0.16,
    "switch_to_upi_intent": 0.09,
    "request_new_mandate": 0.20,
    "escalate_to_human": 0.12,
}


@dataclass(frozen=True, slots=True)
class ActionCost:
    """The full price of taking one action once."""

    paise: int
    attempts_consumed: int
    contacts_customer: bool
    annoyance_units: float


#: `attempts_consumed` comes from the schema, never restated, so the compliance
#: gate here and the one in `eval/simulator.py` can never drift on what counts
#: as a debit attempt even though the surrounding costs are duplicated.
_ACTION_COSTS: dict[str, ActionCost] = {
    "do_nothing": ActionCost(
        paise=0,
        attempts_consumed=ATTEMPTS_CONSUMED["do_nothing"],
        contacts_customer=False,
        annoyance_units=EXPECTED_ANNOYANCE_UNITS["do_nothing"],
    ),
    "retry_now": ActionCost(
        paise=ATTEMPT_COST_PAISE,
        attempts_consumed=ATTEMPTS_CONSUMED["retry_now"],
        contacts_customer=False,
        annoyance_units=EXPECTED_ANNOYANCE_UNITS["retry_now"],
    ),
    "retry_in_2h": ActionCost(
        paise=ATTEMPT_COST_PAISE,
        attempts_consumed=ATTEMPTS_CONSUMED["retry_in_2h"],
        contacts_customer=False,
        annoyance_units=EXPECTED_ANNOYANCE_UNITS["retry_in_2h"],
    ),
    "retry_in_24h": ActionCost(
        paise=ATTEMPT_COST_PAISE,
        attempts_consumed=ATTEMPTS_CONSUMED["retry_in_24h"],
        contacts_customer=False,
        annoyance_units=EXPECTED_ANNOYANCE_UNITS["retry_in_24h"],
    ),
    "retry_next_salary_day": ActionCost(
        paise=ATTEMPT_COST_PAISE,
        attempts_consumed=ATTEMPTS_CONSUMED["retry_next_salary_day"],
        contacts_customer=False,
        annoyance_units=EXPECTED_ANNOYANCE_UNITS["retry_next_salary_day"],
    ),
    "nudge_then_retry": ActionCost(
        paise=WHATSAPP_COST_PAISE + PAYMENT_LINK_COST_PAISE + ATTEMPT_COST_PAISE,
        attempts_consumed=ATTEMPTS_CONSUMED["nudge_then_retry"],
        contacts_customer=True,
        annoyance_units=EXPECTED_ANNOYANCE_UNITS["nudge_then_retry"],
    ),
    "switch_to_upi_intent": ActionCost(
        paise=SMS_COST_PAISE + ATTEMPT_COST_PAISE,
        attempts_consumed=ATTEMPTS_CONSUMED["switch_to_upi_intent"],
        contacts_customer=True,
        annoyance_units=EXPECTED_ANNOYANCE_UNITS["switch_to_upi_intent"],
    ),
    "request_new_mandate": ActionCost(
        paise=NEW_MANDATE_REQUEST_PAISE + SMS_COST_PAISE,
        attempts_consumed=ATTEMPTS_CONSUMED["request_new_mandate"],
        contacts_customer=True,
        annoyance_units=EXPECTED_ANNOYANCE_UNITS["request_new_mandate"],
    ),
    "escalate_to_human": ActionCost(
        paise=HUMAN_ESCALATION_PAISE,
        attempts_consumed=ATTEMPTS_CONSUMED["escalate_to_human"],
        contacts_customer=True,
        annoyance_units=EXPECTED_ANNOYANCE_UNITS["escalate_to_human"],
    ),
}

assert set(_ACTION_COSTS) == set(ACTIONS), "cost table and action space disagree"


def action_cost(action: str) -> ActionCost:
    """Cost of one action. Raises `KeyError` on an unknown action name."""
    try:
        return _ACTION_COSTS[action]
    except KeyError:
        raise KeyError(f"no cost defined for action {action!r}; known: {list(ACTIONS)}") from None


def annoyance_to_paise(annoyance_units: float, *, clv_paise: int = CUSTOMER_LIFETIME_VALUE_PAISE) -> float:
    """Expected lifetime value put at risk by `annoyance_units` of irritation."""
    return annoyance_units * ANNOYANCE_TO_CHURN_PER_UNIT * clv_paise
