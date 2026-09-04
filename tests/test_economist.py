"""Unit tests for the economist layer: EV arithmetic, estimators, and the five
hard compliance rules that arithmetic may never override.

No API key or network is used anywhere in this file - every input is a
hand-built fixture, same as `tests/test_signals.py`.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from retry_economist.economist import APPROVE, APPROVE_TRUNCATED, VETO, Economist, compute_ev  # noqa: E402
from retry_economist.economist.compliance import CONTACT_CAP, apply_compliance  # noqa: E402
from retry_economist.economist.costs import (  # noqa: E402
    ANNOYANCE_TO_CHURN_PER_UNIT,
    ATTEMPT_COST_PAISE,
    CUSTOMER_LIFETIME_VALUE_PAISE,
    EXPECTED_ANNOYANCE_UNITS,
    NEW_MANDATE_REQUEST_PAISE,
    SMS_COST_PAISE,
    VALUE_CAPTURE_RATE,
)
from retry_economist.economist.costs import action_cost as economist_action_cost  # noqa: E402
from retry_economist.economist.estimator import HistoricalPriorEstimator, RouterEstimator  # noqa: E402
from retry_economist.economist.timing import (  # noqa: E402
    DAILY_DISCOUNT_RATE,
    discount,
    expected_days_for_action,
)
from retry_economist.policies.base import ObservedTransaction  # noqa: E402
from retry_economist.router.signals import Signal, Signals  # noqa: E402
from retry_economist.schema import IST  # noqa: E402

BASE = datetime(2026, 6, 10, 12, 0, tzinfo=IST)

#: An amount large enough that, if EV arithmetic could ever outrun a
#: compliance rule, it would show up immediately as a huge positive number.
ABSURD_AMOUNT_PAISE = 10_000_000_000  # INR 10 crore on a single failed payment


def make_txn(
    *,
    txn_id: str = "pay_test",
    customer_id: str = "cust_test",
    created_at: datetime | None = None,
    failure_code: str = "96",
    gateway_message: str = "gateway_error PG-500",
    decline_type: str = "soft",
    issuer: str = "HDFC",
    amount_paise: int = 50_000,
    retry_attempts_used: int = 0,
    retry_cap: int = 3,
    mandate_id: str | None = "mnd_test",
    mandate_expiry: datetime | None = None,
    comms_received_last_7d: int = 1,
) -> ObservedTransaction:
    return ObservedTransaction(
        txn_id=txn_id,
        customer_id=customer_id,
        created_at=created_at or BASE,
        amount_paise=amount_paise,
        method="upi_autopay",
        issuer=issuer,
        is_recurring=True,
        mandate_id=mandate_id,
        mandate_expiry=BASE + timedelta(days=100) if mandate_expiry is None else mandate_expiry,
        retry_attempts_used=retry_attempts_used,
        retry_cap=retry_cap,
        failure_code=failure_code,
        gateway_message=gateway_message,
        decline_type=decline_type,
        issuer_health_at_failure=0.01,
        tenure_days=400,
        past_txn_count=30,
        past_success_rate=0.9,
        prior_failed_attempts_this_invoice=0,
        comms_received_last_7d=comms_received_last_7d,
        preferred_method="upi_autopay",
        city_tier=1,
    )


@dataclass(frozen=True, slots=True)
class FakeProposal:
    """Duck-types `router.router.Proposal` with only what the economist reads."""

    proposed_plan: Tuple[str, ...]
    p_recover_if_act: float = 0.5
    p_recover_if_abstain: float = 0.2
    signals: object | None = None


def _dummy_signals(*, liquidity_days: float | None = None) -> Signals:
    liquidity_detail = {} if liquidity_days is None else {"estimated_days_until_likely_credit": liquidity_days}
    return Signals(
        root_cause=Signal("root_cause", "technical_error", 0.8, "stub", {}),
        issuer_health_now=Signal("issuer_health_now", "normal", 0.5, "stub", {}),
        liquidity_timing=Signal("liquidity_timing", 7, 0.5, "stub", liquidity_detail),
    )


# ---------------------------------------------------------------------------
# EV arithmetic, hand-computed
# ---------------------------------------------------------------------------


def test_ev_matches_a_hand_computation_for_a_single_action_plan() -> None:
    txn = make_txn(amount_paise=50_000)
    proposal = FakeProposal(proposed_plan=("retry_now",), p_recover_if_act=0.8, p_recover_if_abstain=0.3)

    ev = compute_ev(txn, ("retry_now",), proposal, RouterEstimator())

    delta_p = 0.8 - 0.3
    days = expected_days_for_action("retry_now")
    factor = 1.0 / ((1.0 + DAILY_DISCOUNT_RATE) ** days)
    gross = 50_000 * 1.0 * delta_p * factor
    action_cost_paise = ATTEMPT_COST_PAISE
    annoyance_units = EXPECTED_ANNOYANCE_UNITS["retry_now"]
    annoyance_cost = annoyance_units * ANNOYANCE_TO_CHURN_PER_UNIT * CUSTOMER_LIFETIME_VALUE_PAISE
    expected_net = gross - action_cost_paise - annoyance_cost

    assert ev.delta_p == pytest.approx(0.5)
    assert ev.expected_days_to_recovery == pytest.approx(days)
    assert ev.discount_factor == pytest.approx(factor)
    assert ev.gross_value_paise == pytest.approx(gross)
    assert ev.action_cost_paise == action_cost_paise
    assert ev.annoyance_units == pytest.approx(annoyance_units)
    assert ev.annoyance_cost_paise == pytest.approx(annoyance_cost)
    assert ev.net_expected_value_paise == pytest.approx(expected_net)
    assert ev.estimator_label == "router"


def test_ev_costs_sum_across_a_multi_action_plan() -> None:
    txn = make_txn(amount_paise=1_000_000)
    plan = ("nudge_then_retry", "escalate_to_human")
    proposal = FakeProposal(proposed_plan=plan, p_recover_if_act=0.7, p_recover_if_abstain=0.1)

    ev = compute_ev(txn, plan, proposal, RouterEstimator())

    expected_cost = economist_action_cost("nudge_then_retry").paise + economist_action_cost(
        "escalate_to_human"
    ).paise
    expected_annoyance_units = EXPECTED_ANNOYANCE_UNITS["nudge_then_retry"] + EXPECTED_ANNOYANCE_UNITS[
        "escalate_to_human"
    ]
    # Multi-action horizon is the LATEST step's horizon, not the earliest.
    expected_days = max(expected_days_for_action("nudge_then_retry"), expected_days_for_action("escalate_to_human"))

    assert ev.action_cost_paise == expected_cost
    assert ev.annoyance_units == pytest.approx(expected_annoyance_units)
    assert ev.expected_days_to_recovery == pytest.approx(expected_days)


def test_negative_delta_p_produces_negative_ev_and_a_veto() -> None:
    """Acting must never look free: a plan that does WORSE than abstaining is
    unprofitable by construction, whatever it costs."""
    txn = make_txn(amount_paise=50_000)
    proposal = FakeProposal(proposed_plan=("retry_now",), p_recover_if_act=0.2, p_recover_if_abstain=0.6)

    decision = Economist(RouterEstimator()).decide(txn, proposal)

    assert decision.ev is not None
    assert decision.ev.net_expected_value_paise < 0
    assert decision.verdict == VETO
    assert decision.plan == ()


def test_retry_next_salary_day_uses_the_liquidity_signal_for_timing() -> None:
    txn = make_txn(amount_paise=200_000, failure_code="51", gateway_message="INSUFFICIENT_FUNDS")
    signals = _dummy_signals(liquidity_days=9.0)
    proposal = FakeProposal(
        proposed_plan=("retry_next_salary_day",),
        p_recover_if_act=0.9,
        p_recover_if_abstain=0.1,
        signals=signals,
    )

    ev = compute_ev(txn, ("retry_next_salary_day",), proposal, RouterEstimator())

    assert ev.expected_days_to_recovery == pytest.approx(9.0)
    assert ev.discount_factor == pytest.approx(discount(9.0))


# ---------------------------------------------------------------------------
# estimators
# ---------------------------------------------------------------------------


def test_router_estimator_reads_the_proposal_directly() -> None:
    proposal = FakeProposal(proposed_plan=("retry_now",), p_recover_if_act=0.77, p_recover_if_abstain=0.11)
    p_act, p_abstain = RouterEstimator().estimate(make_txn(), proposal)
    assert (p_act, p_abstain) == (0.77, 0.11)


def test_historical_prior_estimator_prefers_the_code_action_pair() -> None:
    estimator = HistoricalPriorEstimator(
        abstain_by_code={"51": 0.25},
        act_by_code_action={("51", "retry_next_salary_day"): 0.6},
        act_by_code={"51": 0.4},
        global_abstain=0.2,
        global_act=0.3,
    )
    txn = make_txn(failure_code="51")
    proposal = FakeProposal(proposed_plan=("retry_next_salary_day",))

    p_act, p_abstain = estimator.estimate(txn, proposal)
    assert p_act == 0.6  # code+action hit, not the code-only fallback
    assert p_abstain == 0.25


def test_historical_prior_estimator_falls_back_when_uncharted() -> None:
    estimator = HistoricalPriorEstimator(
        abstain_by_code={},
        act_by_code_action={},
        act_by_code={},
        global_abstain=0.15,
        global_act=0.35,
    )
    txn = make_txn(failure_code="ZZZ")
    proposal = FakeProposal(proposed_plan=("retry_now",))

    p_act, p_abstain = estimator.estimate(txn, proposal)
    assert p_act == 0.35
    assert p_abstain == 0.15


def test_economist_has_no_hardcoded_default_estimator() -> None:
    """`Estimator` is a required constructor argument - there is no default
    that could silently prefer the router (or the prior) before the
    calibration numbers say which one should win."""
    import inspect

    sig = inspect.signature(Economist.__init__)
    assert sig.parameters["estimator"].default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# C1 - risk declined
# ---------------------------------------------------------------------------


def test_c1_vetoes_a_risk_decline_at_any_expected_value() -> None:
    txn = make_txn(
        failure_code="R05",
        gateway_message="DECLINED BY RISK ENGINE - SUSPECTED FRAUD",
        decline_type="hard",
        amount_paise=ABSURD_AMOUNT_PAISE,
    )
    proposal = FakeProposal(proposed_plan=("escalate_to_human",), p_recover_if_act=0.99, p_recover_if_abstain=0.01)

    decision = Economist(RouterEstimator()).decide(txn, proposal)

    assert decision.verdict == VETO
    assert decision.plan == ()
    assert any(c.rule_id == "C1_RISK_DECLINED" and c.fired for c in decision.compliance.checks)


def test_c1_does_not_fire_on_a_non_risk_decline() -> None:
    result = apply_compliance(make_txn(failure_code="96", decline_type="soft"), ("retry_in_2h",))
    check = next(c for c in result.checks if c.rule_id == "C1_RISK_DECLINED")
    assert not check.fired


# ---------------------------------------------------------------------------
# C2 - hard decline, no debit retry
# ---------------------------------------------------------------------------


def test_c2_strips_debit_actions_but_keeps_request_new_mandate() -> None:
    txn = make_txn(failure_code="41", decline_type="hard", amount_paise=ABSURD_AMOUNT_PAISE)
    plan = ("retry_now", "request_new_mandate")

    result = apply_compliance(txn, plan)

    assert "retry_now" not in result.allowed_plan
    assert "request_new_mandate" in result.allowed_plan


def test_c2_never_lets_a_debit_retry_through_regardless_of_amount() -> None:
    txn = make_txn(failure_code="41", decline_type="hard", amount_paise=ABSURD_AMOUNT_PAISE)
    proposal = FakeProposal(proposed_plan=("retry_now",), p_recover_if_act=0.95, p_recover_if_abstain=0.01)

    decision = Economist(RouterEstimator()).decide(txn, proposal)

    assert "retry_now" not in decision.plan
    assert decision.verdict == VETO  # nothing else was proposed to fall back to


# ---------------------------------------------------------------------------
# C3 - attempt cap double-guard
# ---------------------------------------------------------------------------


def test_c3_drops_whatever_does_not_fit_the_remaining_budget() -> None:
    txn = make_txn(retry_attempts_used=2, retry_cap=3, amount_paise=ABSURD_AMOUNT_PAISE)  # 1 left
    plan = ("retry_now", "retry_in_2h")  # both cost 1 attempt each

    result = apply_compliance(txn, plan)

    assert result.allowed_plan == ("retry_now",)
    check = next(c for c in result.checks if c.rule_id == "C3_ATTEMPT_CAP")
    assert check.fired
    assert check.removed == ("retry_in_2h",)


def test_c3_never_admits_more_debit_attempts_than_remain_regardless_of_amount() -> None:
    txn = make_txn(retry_attempts_used=3, retry_cap=3, amount_paise=ABSURD_AMOUNT_PAISE)  # 0 left
    proposal = FakeProposal(proposed_plan=("retry_now",), p_recover_if_act=0.99, p_recover_if_abstain=0.01)

    decision = Economist(RouterEstimator()).decide(txn, proposal)

    assert "retry_now" not in decision.plan
    assert decision.verdict == VETO


# ---------------------------------------------------------------------------
# C4 - expired mandate
# ---------------------------------------------------------------------------


def test_c4_blocks_debit_before_fresh_consent() -> None:
    txn = make_txn(
        mandate_id="mnd_1",
        mandate_expiry=BASE - timedelta(days=1),
        amount_paise=ABSURD_AMOUNT_PAISE,
    )
    plan = ("retry_now", "request_new_mandate")

    result = apply_compliance(txn, plan)

    assert "retry_now" not in result.allowed_plan
    assert "request_new_mandate" in result.allowed_plan


def test_c4_never_admits_a_debit_on_an_expired_mandate_regardless_of_amount() -> None:
    txn = make_txn(
        mandate_id="mnd_1", mandate_expiry=BASE - timedelta(days=1), amount_paise=ABSURD_AMOUNT_PAISE
    )
    proposal = FakeProposal(proposed_plan=("retry_now",), p_recover_if_act=0.99, p_recover_if_abstain=0.01)

    decision = Economist(RouterEstimator()).decide(txn, proposal)

    assert "retry_now" not in decision.plan
    assert decision.verdict == VETO


# ---------------------------------------------------------------------------
# C5 - contact cap
# ---------------------------------------------------------------------------


def test_c5_blocks_further_contact_once_the_cap_is_reached() -> None:
    txn = make_txn(comms_received_last_7d=CONTACT_CAP, amount_paise=ABSURD_AMOUNT_PAISE)
    plan = ("nudge_then_retry",)

    result = apply_compliance(txn, plan)

    assert result.allowed_plan == ()
    check = next(c for c in result.checks if c.rule_id == "C5_CONTACT_CAP")
    assert check.fired


def test_c5_never_admits_contact_over_the_cap_regardless_of_amount() -> None:
    txn = make_txn(comms_received_last_7d=CONTACT_CAP + 2, amount_paise=ABSURD_AMOUNT_PAISE)
    proposal = FakeProposal(
        proposed_plan=("switch_to_upi_intent",), p_recover_if_act=0.99, p_recover_if_abstain=0.01
    )

    decision = Economist(RouterEstimator()).decide(txn, proposal)

    assert "switch_to_upi_intent" not in decision.plan
    assert decision.verdict == VETO


def test_c5_does_not_fire_below_the_cap() -> None:
    txn = make_txn(comms_received_last_7d=CONTACT_CAP - 1)
    result = apply_compliance(txn, ("nudge_then_retry",))
    check = next(c for c in result.checks if c.rule_id == "C5_CONTACT_CAP")
    assert not check.fired
    assert result.allowed_plan == ("nudge_then_retry",)


# ---------------------------------------------------------------------------
# decision shape: approve / approve-truncated / veto, no rerouting
# ---------------------------------------------------------------------------


def test_full_approval_when_nothing_is_removed_and_ev_is_positive() -> None:
    txn = make_txn(amount_paise=500_000, failure_code="96", decline_type="soft")
    proposal = FakeProposal(proposed_plan=("retry_in_2h",), p_recover_if_act=0.8, p_recover_if_abstain=0.2)

    decision = Economist(RouterEstimator()).decide(txn, proposal)

    assert decision.verdict == APPROVE
    assert decision.plan == ("retry_in_2h",)
    assert decision.plan == decision.proposed_plan


def test_approve_truncated_when_compliance_removes_something_but_the_rest_is_profitable() -> None:
    txn = make_txn(
        failure_code="41",
        decline_type="hard",
        amount_paise=500_000,
        comms_received_last_7d=0,
    )
    plan = ("retry_now", "request_new_mandate")
    proposal = FakeProposal(proposed_plan=plan, p_recover_if_act=0.8, p_recover_if_abstain=0.1)

    decision = Economist(RouterEstimator()).decide(txn, proposal)

    assert decision.verdict == APPROVE_TRUNCATED
    assert decision.plan == ("request_new_mandate",)
    assert decision.proposed_plan == plan
    # No rerouting: everything in the approved plan was already in the proposal.
    assert set(decision.plan) <= set(decision.proposed_plan)


def test_veto_when_router_proposes_nothing() -> None:
    txn = make_txn()
    proposal = FakeProposal(proposed_plan=())

    decision = Economist(RouterEstimator()).decide(txn, proposal)

    assert decision.verdict == VETO
    assert decision.plan == ()
    assert decision.ev is None


def test_the_economist_never_adds_an_action_the_router_did_not_propose() -> None:
    """No rerouting, across every branch: approve, truncate, or veto, the
    approved plan is always a subset of the proposed one."""
    cases = [
        (make_txn(amount_paise=500_000), FakeProposal(proposed_plan=("retry_in_2h",), p_recover_if_act=0.8, p_recover_if_abstain=0.2)),
        (
            make_txn(failure_code="41", decline_type="hard", amount_paise=500_000),
            FakeProposal(proposed_plan=("retry_now", "request_new_mandate"), p_recover_if_act=0.8, p_recover_if_abstain=0.1),
        ),
        (make_txn(amount_paise=500_000), FakeProposal(proposed_plan=("retry_now",), p_recover_if_act=0.1, p_recover_if_abstain=0.9)),
    ]
    for txn, proposal in cases:
        decision = Economist(RouterEstimator()).decide(txn, proposal)
        assert set(decision.plan).issubset(set(proposal.proposed_plan))


# ---------------------------------------------------------------------------
# cost tables: the economist's copy must not drift from eval/costs.py
# ---------------------------------------------------------------------------


def test_cost_tables_do_not_drift_from_the_eval_package() -> None:
    """`economist/costs.py` duplicates `eval/costs.py` rather than importing
    it (see that module's docstring for why). This test is the tripwire: it
    is allowed to import both, because it lives outside every guarded
    package, and it fails the build the moment the two tables disagree.
    """
    from retry_economist.eval import costs as eval_costs
    from retry_economist.schema import ACTIONS

    assert economist_action_cost is not None  # imported above; keeps the linter quiet

    from retry_economist.economist import costs as econ_costs

    assert econ_costs.ATTEMPT_COST_PAISE == eval_costs.ATTEMPT_COST_PAISE
    assert econ_costs.SMS_COST_PAISE == eval_costs.SMS_COST_PAISE
    assert econ_costs.WHATSAPP_COST_PAISE == eval_costs.WHATSAPP_COST_PAISE
    assert econ_costs.PAYMENT_LINK_COST_PAISE == eval_costs.PAYMENT_LINK_COST_PAISE
    assert econ_costs.NEW_MANDATE_REQUEST_PAISE == eval_costs.NEW_MANDATE_REQUEST_PAISE
    assert econ_costs.HUMAN_ESCALATION_PAISE == eval_costs.HUMAN_ESCALATION_PAISE
    assert econ_costs.CUSTOMER_LIFETIME_VALUE_PAISE == eval_costs.CUSTOMER_LIFETIME_VALUE_PAISE
    assert econ_costs.ANNOYANCE_TO_CHURN_PER_UNIT == eval_costs.ANNOYANCE_TO_CHURN_PER_UNIT
    assert econ_costs.VALUE_CAPTURE_RATE == eval_costs.VALUE_CAPTURE_RATE

    for action in ACTIONS:
        econ = econ_costs.action_cost(action)
        ref = eval_costs.action_cost(action)
        assert econ.paise == ref.paise, action
        assert econ.attempts_consumed == ref.attempts_consumed, action
        assert econ.contacts_customer == ref.contacts_customer, action


def test_new_mandate_cost_composition_is_unchanged() -> None:
    cost = economist_action_cost("request_new_mandate")
    assert cost.paise == NEW_MANDATE_REQUEST_PAISE + SMS_COST_PAISE
