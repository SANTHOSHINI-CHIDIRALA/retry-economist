"""The deterministic stand-in's reasoning, kept apart from the transport.

This is what `MockProvider` returns when no responder is injected. It is a fixed
heuristic over the same facts block a real model would receive, and it exists so
that the router, cache, ablation policy and calibration machinery can be run and
asserted with no network and no key.

It is NOT a language model. Wherever its output reaches a report, the report
names the provider, because a number produced here says nothing whatsoever about
what an LLM would do.

The probability estimates are the part worth attention. They are the router's
most valuable output - the economist layer is impossible without them - so they
are derived from the signals rather than pulled from thin air, and they are then
scored against ground truth like any other predictor. Whether they beat a
historical prior is an open question the calibration report answers.
"""

from __future__ import annotations

from typing import Any

# Canonical cause names, matching `router/signals.py`. Compared as strings so
# this module never has to import the router.
FUNDS = "insufficient_funds"
DOWNTIME = "bank_downtime"
COLLECT_TIMEOUT = "upi_collect_timeout"
THREE_DS = "three_ds_dropoff"
EXPIRED_MANDATE = "expired_mandate"
HARD_BLOCK = "issuer_decline_hard"
RISK = "risk_declined"
TECHNICAL = "technical_error"
UNKNOWN = "unknown"

#: Action chosen per cause, and the base chance it clears the block. Deliberately
#: the same shape a payments engineer would reach for; the value added over the
#: rules baseline has to come from the probabilities, not the mapping.
_PLAYBOOK: dict[str, tuple[list[str], float]] = {
    FUNDS: (["retry_next_salary_day"], 0.55),
    DOWNTIME: (["retry_in_24h"], 0.70),
    COLLECT_TIMEOUT: (["switch_to_upi_intent"], 0.58),
    THREE_DS: (["nudge_then_retry"], 0.45),
    EXPIRED_MANDATE: (["request_new_mandate"], 0.50),
    TECHNICAL: (["retry_in_2h"], 0.60),
    HARD_BLOCK: ([], 0.0),
    RISK: ([], 0.0),
    UNKNOWN: (["retry_in_2h"], 0.35),
}

#: Chance the customer pays unaided, before any signal adjustment. Reflects that
#: most soft declines resolve themselves surprisingly often, and hard ones never.
_ABSTAIN_BASE: dict[str, float] = {
    FUNDS: 0.26,
    DOWNTIME: 0.24,
    COLLECT_TIMEOUT: 0.16,
    THREE_DS: 0.18,
    EXPIRED_MANDATE: 0.10,
    TECHNICAL: 0.25,
    HARD_BLOCK: 0.01,
    RISK: 0.01,
    UNKNOWN: 0.20,
}

#: Actions that put a debit on the wire, so the plan can be trimmed to budget.
_ATTEMPT_ACTIONS = frozenset(
    {
        "retry_now",
        "retry_in_2h",
        "retry_in_24h",
        "retry_next_salary_day",
        "nudge_then_retry",
        "switch_to_upi_intent",
    }
)


def _clamp(x: float, lo: float = 0.01, hi: float = 0.97) -> float:
    return lo if x < lo else hi if x > hi else x


def heuristic_proposal(facts: dict[str, Any]) -> dict[str, Any]:
    """Turn a facts block into a proposal in the router's response schema."""
    signals = facts.get("signals", {})
    root = signals.get("root_cause", {})
    issuer = signals.get("issuer_health_now", {})
    liquidity = signals.get("liquidity_timing", {})

    cause = root.get("value", UNKNOWN)
    cause_confidence = float(root.get("confidence", 0.5))
    attempts_left = int(facts.get("attempts_left", 0))

    plan, base_act = _PLAYBOOK.get(cause, _PLAYBOOK[UNKNOWN])
    plan = list(plan)

    # --- adjust the act probability using the signals -----------------------
    p_act = base_act
    spike = float(issuer.get("multiple_over_baseline", 1.0) or 1.0)
    days_to_credit = liquidity.get("estimated_days_until_likely_credit")
    liquidity_confidence = float(liquidity.get("confidence", 0.0) or 0.0)

    if cause == DOWNTIME:
        # A live spike means the incident is probably still running, so a
        # 24-hour wait is more likely to land after it than during it.
        p_act *= 1.0 + min(0.25, 0.08 * max(0.0, spike - 1.0))
    if cause == FUNDS and days_to_credit is not None:
        # Waiting for a credit that is days away beats waiting for one weeks
        # away; the invoice ages either way.
        proximity = max(0.0, 1.0 - min(float(days_to_credit), 21.0) / 21.0)
        p_act *= 0.72 + 0.45 * proximity * max(0.35, liquidity_confidence)

    # An unclear cause means the chosen action is less likely to be the right
    # one, so the estimate is pulled toward the middle rather than left crisp.
    p_act = p_act * (0.55 + 0.45 * cause_confidence)

    # Prior failed attempts on this invoice are evidence the block is sticky.
    prior_failures = int(facts.get("prior_failed_attempts_this_invoice", 0))
    p_act *= max(0.6, 1.0 - 0.12 * prior_failures)

    # --- abstain probability -------------------------------------------------
    p_abstain = _ABSTAIN_BASE.get(cause, 0.2)
    history = float(facts.get("past_success_rate", 0.85))
    p_abstain *= 0.6 + 0.8 * history
    if cause == FUNDS and days_to_credit is not None and float(days_to_credit) <= 3:
        # Payday is imminent; a fair share of these settle themselves.
        p_abstain *= 1.35

    # --- budget trim ---------------------------------------------------------
    if plan and sum(1 for a in plan if a in _ATTEMPT_ACTIONS) > attempts_left:
        plan = [a for a in plan if a not in _ATTEMPT_ACTIONS]
        if not plan:
            p_act = 0.0

    p_act = 0.0 if not plan else _clamp(p_act)
    p_abstain = _clamp(p_abstain)

    rationale = _rationale(cause, plan, issuer, liquidity, cause_confidence)
    return {
        "root_cause": cause,
        "root_cause_confidence": round(cause_confidence, 4),
        "issuer_assessment": str(issuer.get("summary", "no issuer signal")),
        "liquidity_assessment": str(liquidity.get("summary", "no liquidity signal")),
        "proposed_plan": plan,
        "rationale": rationale,
        "p_recover_if_act": round(p_act, 4),
        "p_recover_if_abstain": round(p_abstain, 4),
        "draft_customer_message": _message(cause, plan),
    }


def _rationale(
    cause: str,
    plan: list[str],
    issuer: dict[str, Any],
    liquidity: dict[str, Any],
    confidence: float,
) -> str:
    """Always names a signal - a rationale that cites nothing is decoration."""
    action = plan[0] if plan else "abstain"
    bits = [f"root_cause signal reads {cause} at confidence {confidence:.2f}"]
    if cause == DOWNTIME:
        bits.append(
            f"issuer_health_now shows {issuer.get('multiple_over_baseline', 1.0)}x "
            f"baseline failure volume in the {issuer.get('window_hours', '?')}h window"
        )
    if cause == FUNDS:
        bits.append(
            "liquidity_timing estimates "
            f"{liquidity.get('estimated_days_until_likely_credit')} days to a likely credit "
            f"(confidence {liquidity.get('confidence')})"
        )
    if cause in (HARD_BLOCK, RISK):
        bits.append("no affordable action clears a permanent block, so the plan is empty")
    return f"chose {action}: " + "; ".join(bits)


def _message(cause: str, plan: list[str]) -> str | None:
    if not plan:
        return None
    if cause == FUNDS:
        return "Your payment did not go through. We will try again after your next credit."
    if cause == COLLECT_TIMEOUT:
        return "Your payment request expired. Tap the new request in your UPI app to pay."
    if cause == THREE_DS:
        return "Your card payment was not completed. Here is a fresh link to finish it."
    if cause == EXPIRED_MANDATE:
        return "Your auto-pay mandate has expired. Please re-authorise it to continue."
    return None
