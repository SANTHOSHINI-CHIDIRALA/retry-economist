"""How long a plan takes to pay off, and what that costs in present value.

Money recovered next week is worth less than money recovered today: it sits
unbilled for longer, and the invoice itself grows staler the longer it waits
(late invoices get written off, disputed, or simply forgotten). `discount()`
prices that with a standard exponential discount factor.

`expected_days_to_recovery` is necessarily an estimate, not a measurement - the
economist decides before anything runs, so there is no realised timestamp to
read. Two sources feed it:

- for `retry_next_salary_day`, the router's own `liquidity_timing` signal
  already estimates "how many days until this customer's likely credit date",
  computed from the observed feed alone (see `router/signals.py`) - reusing it
  here means the timing estimate and the action that depends on it can never
  disagree;
- every other action gets a fixed ASSUMPTION, ordered by how long it
  plausibly takes a customer to notice and respond, if they respond at all.
"""

from __future__ import annotations

from typing import Any

#: ASSUMPTION, NOT A MEASUREMENT. Days until an action's outcome is expected to
#: be known. `retry_next_salary_day` is absent on purpose - see
#: `expected_days_for_action`, which derives it per-transaction instead.
_FIXED_EXPECTED_DAYS: dict[str, float] = {
    "do_nothing": 0.0,
    "retry_now": 0.02,  # minutes, not hours
    "retry_in_2h": 0.10,
    "retry_in_24h": 1.0,
    "nudge_then_retry": 1.0,  # customer has to see the nudge before acting
    "switch_to_upi_intent": 0.10,  # redirected immediately; no wait built in
    "request_new_mandate": 4.0,  # registration flow, then the first debit cycle
    "escalate_to_human": 2.0,  # queue time plus a human resolving the case
}

#: Fallback for `retry_next_salary_day` when no liquidity estimate is
#: available at all (should not happen in practice - every proposal carries a
#: `liquidity_timing` signal - but a decision-time function must not raise on
#: missing data). The midpoint of the candidate salary days the signal
#: considers (`router/signals.py::CANDIDATE_SALARY_DAYS`).
_SALARY_DAY_FALLBACK = 11.0

DAILY_DISCOUNT_RATE = 0.02  # ASSUMPTION: working capital cost + invoice staleness


def expected_days_for_action(action: str, *, liquidity_days_until_credit: float | None = None) -> float:
    """Expected days until one action's outcome is known.

    `liquidity_days_until_credit` is the observed-data estimate from
    `Signals.liquidity_timing` (its `estimated_days_until_likely_credit`
    detail field); pass it whenever the action is `retry_next_salary_day` and
    a proposal's signals are on hand.
    """
    if action == "retry_next_salary_day":
        return (
            float(liquidity_days_until_credit)
            if liquidity_days_until_credit is not None
            else _SALARY_DAY_FALLBACK
        )
    try:
        return _FIXED_EXPECTED_DAYS[action]
    except KeyError:
        raise KeyError(f"no expected-days estimate for action {action!r}") from None


def expected_days_for_plan(plan: tuple[str, ...], *, liquidity_days_until_credit: float | None = None) -> float:
    """Expected days until a plan's outcome is known.

    Takes the LATEST of the plan's actions, not the earliest: a plan is a
    waterfall of fallbacks, and money is only in hand once whichever step
    eventually succeeds has run. Using the last action's horizon is the
    conservative (smaller-value) choice for every action after the first.
    """
    if not plan:
        return 0.0
    return max(
        expected_days_for_action(a, liquidity_days_until_credit=liquidity_days_until_credit) for a in plan
    )


def discount(days: float, *, daily_rate: float = DAILY_DISCOUNT_RATE) -> float:
    """Present-value multiplier for money expected `days` from now."""
    return 1.0 / ((1.0 + daily_rate) ** days)


def liquidity_days_from_signals(signals: Any) -> float | None:
    """Pull the estimated days-until-credit out of a `Signals` object.

    Kept as a narrow accessor rather than inlined at every call site, and typed
    loosely (`Any`) so this module does not have to import `router.signals`
    just to name the type - `signals` is always a `router.signals.Signals`
    in practice.
    """
    try:
        return float(signals.liquidity_timing.detail["estimated_days_until_likely_credit"])
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
