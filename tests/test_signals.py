"""Unit tests for the three router signals, against hand-built fixtures.

These are the parts of the router that must be RIGHT rather than plausible, so
they are tested on inputs whose correct answer is known by construction rather
than on whatever the generator happened to produce.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from retry_economist.policies.base import ObservedTransaction  # noqa: E402
from retry_economist.router import signals as sg  # noqa: E402
from retry_economist.schema import IST  # noqa: E402

BASE = datetime(2026, 6, 10, 12, 0, tzinfo=IST)


def make_txn(
    *,
    txn_id: str = "pay_test",
    customer_id: str = "cust_test",
    created_at: datetime | None = None,
    failure_code: str = "51",
    gateway_message: str = "INSUFFICIENT_FUNDS",
    decline_type: str = "soft",
    issuer: str = "HDFC",
    amount_paise: int = 50_000,
    retry_attempts_used: int = 0,
    retry_cap: int = 3,
) -> ObservedTransaction:
    return ObservedTransaction(
        txn_id=txn_id,
        customer_id=customer_id,
        created_at=created_at or BASE,
        amount_paise=amount_paise,
        method="upi_autopay",
        issuer=issuer,
        is_recurring=True,
        mandate_id="mnd_test",
        mandate_expiry=BASE + timedelta(days=100),
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
        comms_received_last_7d=1,
        preferred_method="upi_autopay",
        city_tier=1,
    )


# ---------------------------------------------------------------------------
# (a) root cause
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        ("51", "ERR:51 - NOT SUFFICIENT FUNDS", sg.FUNDS),
        ("91", "Bank is not responding, please try after sometime", sg.DOWNTIME),
        ("U69", "Payer did not respond to collect request", sg.COLLECT_TIMEOUT),
        ("ACS_TIMEOUT", "otp page abandoned by cardholder", sg.THREE_DS),
        ("MANDATE_EXPIRED_M06", "Umrn not active / mandate validity over", sg.EXPIRED_MANDATE),
        ("41", "Do not honour - card reported lost/stolen", sg.HARD_BLOCK),
        ("R05", "R05 -- SUSPECTED FRAUD, BLOCKED", sg.RISK),
        ("96", "ERR96 : SYSTEM MALFUNCTION", sg.TECHNICAL),
    ],
)
def test_code_and_message_agreeing_gives_high_confidence(
    code: str, message: str, expected: str
) -> None:
    signal = sg.root_cause_signal(make_txn(failure_code=code, gateway_message=message))
    assert signal.value == expected
    assert signal.confidence >= 0.9
    assert signal.detail["agreement"] is True


def test_messy_message_variants_all_normalise() -> None:
    """The whole point of the signal: the feed's casing is not consistent."""
    for message in (
        "INSUFFICIENT_FUNDS",
        "Insufficient balance in account",
        "DEBIT FAILED - LOW BAL",
        "acct bal too low for txn amt",
    ):
        assert sg.message_cause(message) == sg.FUNDS


def test_conflicting_code_and_message_is_reported_at_low_confidence() -> None:
    """A mismatch is the case where one confident answer would be wrong."""
    signal = sg.root_cause_signal(
        make_txn(failure_code="51", gateway_message="Bank is not responding, try later")
    )
    assert signal.value == sg.FUNDS  # the code is the more authoritative field
    assert signal.confidence <= 0.35
    assert "CONFLICT" in signal.summary
    assert signal.detail["cause_from_message"] == sg.DOWNTIME
    assert signal.detail["agreement"] is False


def test_unknown_code_falls_back_to_the_message() -> None:
    signal = sg.root_cause_signal(
        make_txn(failure_code="ZZ99", gateway_message="acct bal too low for txn amt")
    )
    assert signal.value == sg.FUNDS
    assert 0.4 < signal.confidence < 0.7


def test_recognising_nothing_is_admitted_rather_than_guessed() -> None:
    signal = sg.root_cause_signal(make_txn(failure_code="ZZ99", gateway_message="qwerty"))
    assert signal.value == sg.UNKNOWN
    assert signal.confidence <= 0.2


# ---------------------------------------------------------------------------
# (b) issuer health
# ---------------------------------------------------------------------------


def test_issuer_spike_is_detected_against_its_own_baseline() -> None:
    """Twenty failures inside one hour, against a fortnight of background."""
    spread = [
        make_txn(txn_id=f"bg_{i}", created_at=BASE + timedelta(hours=6 * i), issuer="SBIN")
        for i in range(40)
    ]
    burst_at = BASE + timedelta(days=3)
    burst = [
        make_txn(
            txn_id=f"burst_{i}",
            created_at=burst_at + timedelta(minutes=2 * i),
            issuer="SBIN",
            failure_code="91",
        )
        for i in range(20)
    ]
    index = sg.SignalIndex(spread + burst)

    during = index.issuer_health_signal(burst[10])
    assert during.value == "degraded"
    assert during.detail["multiple_over_baseline"] > 3.0
    assert during.detail["failures_in_window"] >= 20
    assert during.confidence >= 0.7

    quiet = index.issuer_health_signal(spread[0])
    assert quiet.value in ("normal", "elevated")
    assert quiet.detail["multiple_over_baseline"] < during.detail["multiple_over_baseline"]


def test_issuer_with_no_history_reports_low_confidence_not_a_guess() -> None:
    index = sg.SignalIndex([make_txn(issuer="YESB")])
    signal = index.issuer_health_signal(make_txn(issuer="YESB"))
    assert signal.value == "unknown"
    assert signal.confidence <= 0.15
    assert signal.detail["multiple_over_baseline"] is None


def test_issuer_signal_does_not_read_the_simulator_world() -> None:
    """The signal must be derived from the feed, not from the true downtime.

    Asserted by construction: two transactions with identical observed fields
    but different neighbours must produce different health readings.
    """
    lonely = sg.SignalIndex(
        [make_txn(txn_id=f"x{i}", created_at=BASE + timedelta(days=i), issuer="ICIC")
         for i in range(10)]
    )
    crowded = sg.SignalIndex(
        [make_txn(txn_id=f"y{i}", created_at=BASE + timedelta(minutes=i), issuer="ICIC")
         for i in range(10)]
    )
    probe = make_txn(txn_id="probe", created_at=BASE, issuer="ICIC")
    assert (
        lonely.issuer_health_signal(probe).detail["multiple_over_baseline"]
        != crowded.issuer_health_signal(probe).detail["multiple_over_baseline"]
    )


# ---------------------------------------------------------------------------
# (c) liquidity timing
# ---------------------------------------------------------------------------


def test_salary_day_is_inferred_from_the_failure_pattern() -> None:
    """Failures cluster just BEFORE a credit, so they identify the payday."""
    # A customer whose payments fail on the 27th-30th: paid on the 1st.
    days = (27, 28, 29, 30, 28, 29)
    txns = [
        make_txn(
            txn_id=f"f{i}",
            customer_id="cust_month_end",
            created_at=datetime(2026, 6, d, 10, 0, tzinfo=IST),
        )
        for i, d in enumerate(days)
    ]
    index = sg.SignalIndex(txns)
    signal = index.liquidity_signal(txns[0])

    assert signal.detail["inferred_salary_day"] == 1
    assert signal.detail["estimated_days_until_likely_credit"] <= 4
    assert signal.detail["prior_failures_observed"] == len(days) - 1
    assert signal.confidence > 0.3


def test_a_customer_with_no_history_gets_low_confidence() -> None:
    """One failure cannot support a cycle inference, and the signal says so."""
    txn = make_txn(customer_id="cust_new", created_at=datetime(2026, 6, 12, 9, 0, tzinfo=IST))
    index = sg.SignalIndex([txn])
    signal = index.liquidity_signal(txn)

    assert signal.detail["prior_failures_observed"] == 0
    assert signal.confidence <= 0.15
    assert "population prior" in signal.summary


def test_confidence_grows_with_evidence() -> None:
    def confidence_for(count: int) -> float:
        txns = [
            make_txn(
                txn_id=f"g{i}",
                customer_id="cust_growing",
                created_at=datetime(2026, 6, 27 + (i % 3), 10, 0, tzinfo=IST),
            )
            for i in range(count)
        ]
        return sg.SignalIndex(txns).liquidity_signal(txns[0]).confidence

    assert confidence_for(1) < confidence_for(4) < confidence_for(9)


def test_days_until_wraps_around_the_month() -> None:
    assert sg._days_until_day_of_month(28, 1) == 3
    assert sg._days_until_day_of_month(1, 7) == 6
    assert sg._days_until_day_of_month(7, 7) == 30


def test_signals_bundle_is_json_serialisable() -> None:
    """The bundle goes straight into a prompt, so it must render cleanly."""
    import json

    txn = make_txn()
    bundle = sg.SignalIndex([txn]).signals_for(txn)
    payload = json.loads(json.dumps(bundle.to_dict()))
    assert set(payload) == {"root_cause", "issuer_health_now", "liquidity_timing"}
    for signal in payload.values():
        assert 0.0 <= signal["confidence"] <= 1.0
        assert signal["summary"]
