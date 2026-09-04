"""Tests for the Phase 6 audit ledger: append-only writes, full decision
provenance on one line, idempotent per-action keys, and no secret material
ever reaching disk.

No API key or network is used anywhere in this file - every input is a
hand-built fixture, same as `tests/test_economist.py`, whose `make_txn` and
`_fitted_estimator` helpers this file mirrors rather than imports (this
repo's convention - see `test_router.py` and `test_economist.py`, which each
keep their own copy rather than sharing a fixtures module).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from retry_economist.audit.ledger import (  # noqa: E402
    AuditLedger,
    audit_policy_run,
    idempotency_key,
)
from retry_economist.economist.estimator import HistoricalPriorEstimator  # noqa: E402
from retry_economist.policies.base import ObservedTransaction  # noqa: E402
from retry_economist.policies.retry_economist_naive_plan import (  # noqa: E402
    RetryEconomistNaivePlanPolicy,
)
from retry_economist.policies.retry_economist_prior import RetryEconomistPriorPolicy  # noqa: E402
from retry_economist.router.signals import SignalIndex  # noqa: E402
from retry_economist.schema import IST  # noqa: E402

BASE = datetime(2026, 6, 10, 12, 0, tzinfo=IST)


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


def _fitted_estimator() -> HistoricalPriorEstimator:
    return HistoricalPriorEstimator(
        abstain_by_code={}, act_by_code_action={}, act_by_code={}, global_abstain=0.2, global_act=0.4
    )


# ---------------------------------------------------------------------------
# append-only
# ---------------------------------------------------------------------------


def test_append_only_never_rewrites_earlier_bytes(tmp_path: Path) -> None:
    """A second run's writes must be pure appends: everything already on disk
    survives byte-for-byte, which is what makes this a ledger rather than a
    file that merely happens to be JSONL today."""
    path = tmp_path / "ledger.jsonl"
    ledger = AuditLedger(path)

    ledger.append({"record": 1})
    first_run_bytes = path.read_bytes()

    ledger.append({"record": 2})
    second_run_bytes = path.read_bytes()

    assert second_run_bytes.startswith(first_run_bytes)
    assert len(second_run_bytes) > len(first_run_bytes)


def test_append_creates_the_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "ledger.jsonl"
    AuditLedger(path).append({"ok": True})
    assert path.exists()


# ---------------------------------------------------------------------------
# idempotency keys
# ---------------------------------------------------------------------------


def test_idempotency_key_is_a_pure_function_of_its_three_inputs() -> None:
    assert idempotency_key("pay_1", "retry_now", 0) == idempotency_key("pay_1", "retry_now", 0)
    assert idempotency_key("pay_1", "retry_now", 0) != idempotency_key("pay_1", "retry_now", 1)
    assert idempotency_key("pay_1", "retry_now", 0) != idempotency_key("pay_2", "retry_now", 0)
    assert idempotency_key("pay_1", "retry_now", 0) != idempotency_key("pay_1", "retry_in_2h", 0)


def test_idempotency_keys_are_stable_across_two_separate_runs(tmp_path: Path) -> None:
    """The whole point of the key: replaying the same transaction through a
    fresh policy instance (a restarted process, a re-run tomorrow) must
    recognise the same authorised action as the same action, not a new one."""
    txn = make_txn(failure_code="96", decline_type="soft", retry_attempts_used=0, retry_cap=3)

    def decide_and_audit(ledger_path: Path) -> dict:
        policy = RetryEconomistNaivePlanPolicy(SignalIndex([txn]), _fitted_estimator())
        policy.decide(txn)
        records = audit_policy_run(AuditLedger(ledger_path), policy, [txn])
        return records[0]

    first = decide_and_audit(tmp_path / "run_a.jsonl")
    second = decide_and_audit(tmp_path / "run_b.jsonl")

    assert first["idempotency_keys"] == second["idempotency_keys"]
    assert first["authorised_plan"] == second["authorised_plan"] == [
        "retry_now",
        "retry_in_2h",
        "retry_in_24h",
    ]


def test_idempotency_index_reflects_the_final_authorised_plan_not_the_proposal(
    tmp_path: Path,
) -> None:
    """`attempt_index` numbers positions in what will actually run - the plan
    AFTER compliance - not positions in the plan source's original proposal.
    A cap of 2 lets only the ladder's first two actions through; the surviving
    actions must be indexed 0 and 1, contiguously, not 0 and 2."""
    txn = make_txn(failure_code="96", decline_type="soft", retry_attempts_used=0, retry_cap=2)
    policy = RetryEconomistNaivePlanPolicy(SignalIndex([txn]), _fitted_estimator())
    policy.decide(txn)

    records = audit_policy_run(AuditLedger(tmp_path / "ledger.jsonl"), policy, [txn])
    record = records[0]

    assert record["authorised_plan"] == ["retry_now", "retry_in_2h"]
    assert [k["attempt_index"] for k in record["idempotency_keys"]] == [0, 1]
    assert [k["action"] for k in record["idempotency_keys"]] == ["retry_now", "retry_in_2h"]


# ---------------------------------------------------------------------------
# one record per decision, all valid JSON, keys unique within a run
# ---------------------------------------------------------------------------


def _decide_several(policy, txns) -> None:
    for txn in txns:
        policy.decide(txn)


def test_exactly_one_record_per_decision_and_every_line_is_valid_json(tmp_path: Path) -> None:
    txns = [
        make_txn(txn_id=f"pay_{i}", failure_code="96", decline_type="soft", retry_cap=3)
        for i in range(5)
    ]
    policy = RetryEconomistNaivePlanPolicy(SignalIndex(txns), _fitted_estimator())
    _decide_several(policy, txns)

    ledger_path = tmp_path / "ledger.jsonl"
    records = audit_policy_run(AuditLedger(ledger_path), policy, txns)
    assert len(records) == len(txns)

    lines = ledger_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(txns)
    for line in lines:
        parsed = json.loads(line)  # raises if invalid - the assertion IS the parse
        assert parsed["txn_id"] in {t.txn_id for t in txns}


def test_keys_unique_within_a_run(tmp_path: Path) -> None:
    """Multi-action plans (naive_retry_3x's three-step ladder) across several
    transactions must never collide, even though every plan has the SAME
    action names at the SAME positions - only `txn_id` differs."""
    txns = [
        make_txn(txn_id=f"pay_{i}", failure_code="96", decline_type="soft", retry_cap=3)
        for i in range(10)
    ]
    policy = RetryEconomistNaivePlanPolicy(SignalIndex(txns), _fitted_estimator())
    _decide_several(policy, txns)

    records = audit_policy_run(AuditLedger(tmp_path / "ledger.jsonl"), policy, txns)
    all_keys = [entry["key"] for record in records for entry in record["idempotency_keys"]]

    assert len(all_keys) == 10 * 3  # three approved actions per transaction
    assert len(set(all_keys)) == len(all_keys)


# ---------------------------------------------------------------------------
# full provenance on one line
# ---------------------------------------------------------------------------


def test_record_carries_full_decision_provenance(tmp_path: Path) -> None:
    """A reviewer must be able to answer WHY a rupee was or was not spent from
    one line, without reading the code - so every field the module docstring
    promises has to actually be there."""
    txn = make_txn(failure_code="41", decline_type="hard")  # exercises a real veto path (C2)
    policy = RetryEconomistNaivePlanPolicy(SignalIndex([txn]), _fitted_estimator())
    policy.decide(txn)

    record = audit_policy_run(AuditLedger(tmp_path / "ledger.jsonl"), policy, [txn])[0]

    assert record["txn_id"] == txn.txn_id
    assert record["decided_at"]  # non-empty ISO timestamp
    assert record["policy"] == RetryEconomistNaivePlanPolicy.name
    assert record["estimator"] == "historical_prior_train_only"
    assert set(record["provider"]) == {"label", "model"}

    for signal_name in ("root_cause", "issuer_health_now", "liquidity_timing"):
        assert "confidence" in record["signals"][signal_name]

    assert record["proposal"]["plan"] == ["retry_now", "retry_in_2h", "retry_in_24h"]
    assert "rationale" in record["proposal"]
    assert "p_recover_if_act" in record["proposal"]
    assert "p_recover_if_abstain" in record["proposal"]

    rule_ids = {check["rule_id"] for check in record["compliance"]["checks"]}
    assert rule_ids == {
        "C1_RISK_DECLINED",
        "C2_HARD_DECLINE_NO_DEBIT",
        "C3_ATTEMPT_CAP",
        "C4_EXPIRED_MANDATE",
        "C5_CONTACT_CAP",
    }
    fired = {check["rule_id"] for check in record["compliance"]["checks"] if check["fired"]}
    assert "C2_HARD_DECLINE_NO_DEBIT" in fired  # hard decline: every debit action stripped

    # A hard decline strips the WHOLE naive ladder (no request_new_mandate in
    # it), so this transaction should end in a veto with nothing authorised.
    assert record["verdict"] == "veto"
    assert record["authorised_plan"] == []
    assert record["idempotency_keys"] == []
    assert record["reason"]  # human-readable, non-empty


def test_ev_terms_are_itemised_not_just_a_total(tmp_path: Path) -> None:
    """An approved plan's `ev` block must carry every line of the formula, so
    a reviewer can check the arithmetic itself rather than trust a total."""
    txn = make_txn(failure_code="96", decline_type="soft", retry_cap=3)
    policy = RetryEconomistPriorPolicy(SignalIndex([txn]), _fitted_estimator())
    policy.decide(txn)

    record = audit_policy_run(AuditLedger(tmp_path / "ledger.jsonl"), policy, [txn])[0]
    if record["ev"] is None:
        pytest.skip("this fixture's plan happened to veto on EV; not the path under test")

    for field in (
        "plan",
        "amount_paise",
        "value_capture_rate",
        "p_recover_if_act",
        "p_recover_if_abstain",
        "delta_p",
        "expected_days_to_recovery",
        "daily_discount_rate",
        "discount_factor",
        "gross_value_paise",
        "action_cost_paise",
        "annoyance_units",
        "annoyance_cost_paise",
        "net_expected_value_paise",
    ):
        assert field in record["ev"], f"missing EV line item: {field}"


# ---------------------------------------------------------------------------
# no secret material in the ledger - extends test_router.py's cache test
# ---------------------------------------------------------------------------


def test_no_secret_is_written_to_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors `test_router.py::test_no_secret_is_written_to_the_cache`: key
    material must never reach ANY artefact this project writes to disk, and
    the ledger is a new one. Nothing here reads the env var directly - the
    point is exactly that it shouldn't need to, and shouldn't leak in even by
    accident."""
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-value-do-not-persist")

    txns = [
        make_txn(txn_id="pay_soft", failure_code="96", decline_type="soft"),
        make_txn(txn_id="pay_hard", failure_code="41", decline_type="hard"),
    ]
    policy = RetryEconomistNaivePlanPolicy(SignalIndex(txns), _fitted_estimator())
    _decide_several(policy, txns)

    ledger_path = tmp_path / "ledger.jsonl"
    audit_policy_run(AuditLedger(ledger_path), policy, txns)

    text = ledger_path.read_text(encoding="utf-8")
    assert "super-secret-value-do-not-persist" not in text
    assert "GEMINI_API_KEY" not in text
