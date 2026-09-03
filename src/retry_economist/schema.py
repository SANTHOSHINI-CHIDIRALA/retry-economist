"""Data contract shared by the generator, and later by the router and economist.

The single most important thing this module encodes is the *observable /
latent* boundary. The whole project is an uplift-estimation problem: if any
latent field (true intent, liquidity, salary day, hard-block status, or the
counterfactual outcome itself) leaks into the agent-visible feed, the benchmark
silently becomes trivial and every downstream result is meaningless. So the
boundary lives here, in one place, as explicit key lists that the test suite
asserts against rather than as a convention spread across writers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

#: All timestamps in the simulation are IST. Indian salary cycles, business
#: hours and bank downtime windows only make sense in local time, and storing
#: anything else would force every consumer to re-derive the offset.
IST = timezone(timedelta(hours=5, minutes=30), name="IST")

SIM_START = datetime(2026, 6, 1, 0, 0, 0, tzinfo=IST)
SIM_END = datetime(2026, 7, 16, 0, 0, 0, tzinfo=IST)  # exclusive: 45 whole days
SIM_DAYS = (SIM_END - SIM_START).days

#: Outcomes are simulated this far past a failure. Anything slower than this is
#: worthless to a recovery agent anyway — the invoice has already churned.
RECOVERY_HORIZON_HOURS = 30 * 24

# ---------------------------------------------------------------------------
# Action space
# ---------------------------------------------------------------------------

Action = Literal[
    "do_nothing",
    "retry_now",
    "retry_in_2h",
    "retry_in_24h",
    "retry_next_salary_day",
    "nudge_then_retry",
    "switch_to_upi_intent",
    "request_new_mandate",
    "escalate_to_human",
]

ACTIONS: tuple[Action, ...] = (
    "do_nothing",
    "retry_now",
    "retry_in_2h",
    "retry_in_24h",
    "retry_next_salary_day",
    "nudge_then_retry",
    "switch_to_upi_intent",
    "request_new_mandate",
    "escalate_to_human",
)

#: Actions that put another automated authorisation on the wire, and therefore
#: burn one of the customer's scarce retry attempts.
RETRY_ACTIONS: tuple[Action, ...] = (
    "retry_now",
    "retry_in_2h",
    "retry_in_24h",
    "retry_next_salary_day",
)

#: Debit attempts each action puts on the wire. Scheme and mandate rules cap
#: these per invoice, so this is the quantity the compliance gate meters - it is
#: a property of the action itself, not of any particular cost model, and both
#: the evaluation cost table and any cap-aware policy derive from this one dict.
ATTEMPTS_CONSUMED: dict[str, int] = {
    "do_nothing": 0,
    "retry_now": 1,
    "retry_in_2h": 1,
    "retry_in_24h": 1,
    "retry_next_salary_day": 1,
    "nudge_then_retry": 1,  # the reminder is free; the retry behind it is not
    "switch_to_upi_intent": 1,
    "request_new_mandate": 0,  # collects consent; no debit is attempted
    "escalate_to_human": 0,  # taken off the automated rail entirely
}

Method = Literal[
    "upi_collect",
    "upi_intent",
    "card",
    "netbanking",
    "upi_autopay",
    "enach",
]

DeclineType = Literal["soft", "hard"]
CityTier = Literal[1, 2, 3]

# ---------------------------------------------------------------------------
# The observable / latent boundary
# ---------------------------------------------------------------------------

#: Exactly the keys written to observed.jsonl. The router may condition on
#: these and nothing else.
OBSERVED_FIELDS: tuple[str, ...] = (
    # transaction
    "txn_id",
    "customer_id",
    "created_at",
    "amount_paise",
    "method",
    "issuer",
    "is_recurring",
    "mandate_id",
    "mandate_expiry",
    "retry_attempts_used",
    "retry_cap",
    "failure_code",
    "gateway_message",
    "decline_type",
    "issuer_health_at_failure",
    # customer profile the PSP genuinely has on file
    "tenure_days",
    "past_txn_count",
    "past_success_rate",
    "prior_failed_attempts_this_invoice",
    "comms_received_last_7d",
    "preferred_method",
    "city_tier",
)

#: Keys that must never appear in observed.jsonl. Kept as data so the test
#: suite can assert the ban directly instead of trusting the writer.
FORBIDDEN_OBSERVED_FIELDS: tuple[str, ...] = (
    "intent_to_pay",
    "liquidity",
    "salary_day",
    "annoyance",
    "hard_blocked",
    "would_pay_anyway",
    "failure_mode",
    "latent",
    "outcomes",
)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Transaction:
    """One failed payment attempt, as the PSP would have logged it."""

    txn_id: str
    customer_id: str
    created_at: datetime
    amount_paise: int
    method: Method
    issuer: str
    is_recurring: bool
    mandate_id: str | None
    mandate_expiry: datetime | None
    retry_attempts_used: int
    retry_cap: int
    failure_mode: str  # FailureMode.name — latent label, oracle-side only
    failure_code: str
    gateway_message: str
    decline_type: DeclineType
    issuer_health_at_failure: float

    @property
    def attempts_left(self) -> int:
        return max(0, self.retry_cap - self.retry_attempts_used)


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    """What happens to one transaction under one action, in the oracle world."""

    recovered: bool
    hours_to_recovery: float | None
    attempts_consumed: int
    customer_annoyance_delta: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "recovered": self.recovered,
            "hours_to_recovery": (
                None if self.hours_to_recovery is None else round(self.hours_to_recovery, 3)
            ),
            "attempts_consumed": self.attempts_consumed,
            "customer_annoyance_delta": round(self.customer_annoyance_delta, 4),
        }


@dataclass(frozen=True, slots=True)
class OracleRecord:
    """Counterfactual outcomes plus the latent state that produced them."""

    txn_id: str
    customer_id: str
    failure_mode: str
    would_pay_anyway: bool
    latent: dict[str, Any]
    outcomes: dict[str, ActionOutcome] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "txn_id": self.txn_id,
            "customer_id": self.customer_id,
            "failure_mode": self.failure_mode,
            "would_pay_anyway": self.would_pay_anyway,
            "latent": self.latent,
            "outcomes": {a: self.outcomes[a].to_dict() for a in ACTIONS if a in self.outcomes},
        }


def iso(ts: datetime | None) -> str | None:
    """Serialise a timestamp; `None` stays `None` so JSON stays round-trippable."""
    return None if ts is None else ts.isoformat()


def transaction_public_fields(txn: Transaction) -> dict[str, Any]:
    """Transaction half of the observed record (drops the latent `failure_mode`)."""
    d = asdict(txn)
    d["created_at"] = iso(txn.created_at)
    d["mandate_expiry"] = iso(txn.mandate_expiry)
    d["issuer_health_at_failure"] = round(txn.issuer_health_at_failure, 4)
    d.pop("failure_mode")
    return d
