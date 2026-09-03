"""Failure taxonomy and the causal generation of failed transactions.

Two things here matter more than anything else in the file.

First, the gateway messages are deliberately filthy: inconsistent casing,
abbreviations, vendor-specific prefixes, three or more phrasings for the same
underlying condition. Real acquirer feeds look like this, and a router that only
works against a clean enum is not solving the actual problem. The clean
`failure_code` is published alongside precisely so we can measure whether
reasoning over the messy string adds anything.

Second, failure modes are *caused*, never drawn uniformly. A dry account on the
28th produces INSUFFICIENT_FUNDS; a payment fired into a Yes Bank outage
produces BANK_DOWNTIME; a collect request to someone who has decided not to pay
times out. Because the cause is real, curing the cause (waiting for payday,
waiting out the outage, nudging) genuinely works - which is the only reason an
intelligent router can beat a fixed retry schedule on this dataset.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from random import Random

from retry_economist.generator.customers import Customer
from retry_economist.generator.world import ISSUER_SHARE, World
from retry_economist.schema import SIM_DAYS, SIM_START, DeclineType, Method, Transaction


class FailureMode(Enum):
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    BANK_DOWNTIME = "BANK_DOWNTIME"
    UPI_COLLECT_TIMEOUT = "UPI_COLLECT_TIMEOUT"
    THREE_DS_DROPOFF = "THREE_DS_DROPOFF"
    EXPIRED_MANDATE = "EXPIRED_MANDATE"
    ISSUER_DECLINE_HARD = "ISSUER_DECLINE_HARD"
    RISK_DECLINED = "RISK_DECLINED"
    TECHNICAL_ERROR = "TECHNICAL_ERROR"


@dataclass(frozen=True, slots=True)
class FailureSpec:
    """Static facts about a failure mode, as an acquirer would document them."""

    code: str
    decline_type: DeclineType
    retryable_after_hours: float
    #: EXPIRED_MANDATE is soft in the sense that the customer is fine and the
    #: money exists, but no retry can succeed until fresh consent is collected.
    #: It gets its own flag rather than a third decline_type so the published
    #: schema stays the soft/hard binary that real gateways expose.
    requires_new_consent: bool
    messages: tuple[str, ...]


#: At least three message variants per mode. The inconsistency is the point.
SPECS: dict[FailureMode, FailureSpec] = {
    FailureMode.INSUFFICIENT_FUNDS: FailureSpec(
        code="51",
        decline_type="soft",
        retryable_after_hours=48.0,
        requires_new_consent=False,
        messages=(
            "INSUFFICIENT_FUNDS",
            "Insufficient balance in account",
            "ERR:51 - NOT SUFFICIENT FUNDS",
            "DEBIT FAILED - LOW BAL",
            "acct bal too low for txn amt",
        ),
    ),
    FailureMode.BANK_DOWNTIME: FailureSpec(
        code="91",
        decline_type="soft",
        retryable_after_hours=3.0,
        requires_new_consent=False,
        messages=(
            "ISSUER_UNAVAILABLE",
            "Bank is not responding, please try after sometime",
            "ERR 91 :: ISSUER INOPERATIVE",
            "upstream timeout at remitter bank",
            "NPCI - RB NOT AVAILABLE",
        ),
    ),
    FailureMode.UPI_COLLECT_TIMEOUT: FailureSpec(
        code="U69",
        decline_type="soft",
        retryable_after_hours=6.0,
        requires_new_consent=False,
        messages=(
            "COLLECT_REQUEST_EXPIRED",
            "Payer did not respond to collect request",
            "U69: TXN EXPIRED / NO ACTION FROM PAYER",
            "upi collect timed out (no approval in 300s)",
        ),
    ),
    FailureMode.THREE_DS_DROPOFF: FailureSpec(
        code="ACS_TIMEOUT",
        decline_type="soft",
        retryable_after_hours=4.0,
        requires_new_consent=False,
        messages=(
            "3DS_AUTH_ABANDONED",
            "Customer did not complete OTP verification",
            "ACS TIMEOUT - AUTHENTICATION NOT COMPLETED",
            "otp page abandoned by cardholder",
        ),
    ),
    FailureMode.EXPIRED_MANDATE: FailureSpec(
        code="MANDATE_EXPIRED_M06",
        decline_type="soft",
        retryable_after_hours=0.0,  # no delay helps; only fresh consent does
        requires_new_consent=True,
        messages=(
            "MANDATE_EXPIRED",
            "Umrn not active / mandate validity over",
            "M06 - MANDATE NOT FOUND OR EXPIRED",
            "e-mandate lapsed, re-registration reqd",
        ),
    ),
    FailureMode.ISSUER_DECLINE_HARD: FailureSpec(
        code="41",
        decline_type="hard",
        retryable_after_hours=0.0,
        requires_new_consent=True,
        messages=(
            "CARD_BLOCKED_BY_ISSUER",
            "Do not honour - card reported lost/stolen",
            "ERR41: PICK UP CARD",
            "ACCOUNT CLOSED BY CUSTOMER",
            "instrument permanently blocked",
        ),
    ),
    FailureMode.RISK_DECLINED: FailureSpec(
        code="R05",
        decline_type="hard",
        retryable_after_hours=0.0,
        requires_new_consent=False,
        messages=(
            "RISK_DECLINE",
            "Transaction declined by risk engine",
            "R05 -- SUSPECTED FRAUD, BLOCKED",
            "declined: velocity rule breach",
        ),
    ),
    FailureMode.TECHNICAL_ERROR: FailureSpec(
        code="96",
        decline_type="soft",
        retryable_after_hours=1.0,
        requires_new_consent=False,
        messages=(
            "GATEWAY_ERROR",
            "System malfunction, please retry",
            "ERR96 : SYSTEM MALFUNCTION",
            "internal svc err (ref: PG-500)",
        ),
    ),
}

#: Methods that pull money without the customer present. Their failures are
#: dominated by whether the balance was there, since nobody is watching.
_AUTO_DEBIT: frozenset[Method] = frozenset({"upi_autopay", "enach"})
_MANDATE_METHODS: frozenset[Method] = _AUTO_DEBIT

#: How exposed each rail is to a dry account. Push rails score lower not because
#: balances matter less but because a payer who can see the balance abandons the
#: request instead of letting it decline.
_FUNDS_SENSITIVITY: dict[Method, float] = {
    "upi_autopay": 1.00,
    "enach": 1.00,
    "card": 0.85,
    "netbanking": 0.80,
    "upi_intent": 0.55,
    "upi_collect": 0.45,
}

#: Business-hour weighting: an Indian consumer payment curve, with a late
#: morning bump and a heavier evening peak.
_HOUR_WEIGHTS: tuple[float, ...] = (
    0.3, 0.2, 0.15, 0.15, 0.2, 0.4, 0.9, 1.6, 2.6, 3.6, 4.4, 4.6,
    4.2, 3.6, 3.4, 3.6, 4.0, 4.6, 5.2, 5.4, 4.6, 3.2, 1.8, 0.8,
)

#: Share of failures sampled from inside a live outage window. A dataset of
#: *failed* payments over-represents incidents by construction - during an
#: outage almost everything fails - and without this oversampling a 45-day
#: calendar with a handful of outage hours would yield too few BANK_DOWNTIME
#: rows for timing strategies to be measurable.
_INCIDENT_SAMPLE_RATE = 0.22


def gateway_message(mode: FailureMode, rng: Random) -> str:
    return rng.choice(SPECS[mode].messages)


def _pick_issuer(rng: Random) -> str:
    return rng.choices(list(ISSUER_SHARE), weights=list(ISSUER_SHARE.values()), k=1)[0]


def _business_hour_timestamp(rng: Random) -> datetime:
    day = rng.randrange(SIM_DAYS)
    ts = SIM_START + timedelta(days=day)
    # Weekends are quieter, but not empty - autopay debits do not take Sundays off.
    if ts.weekday() >= 5 and rng.random() < 0.28:
        day = rng.randrange(SIM_DAYS)
    hour = rng.choices(range(24), weights=_HOUR_WEIGHTS, k=1)[0]
    return SIM_START + timedelta(
        days=day, hours=hour, minutes=rng.randrange(60), seconds=rng.randrange(60)
    )


def _sample_created_at(rng: Random, world: World, issuer: str) -> datetime:
    """Pick a failure timestamp, oversampling live incidents for `issuer`."""
    windows = world.issuers[issuer].downtime
    if windows and rng.random() < _INCIDENT_SAMPLE_RATE:
        window = rng.choices(
            list(windows), weights=[(w.end - w.start).total_seconds() for w in windows], k=1
        )[0]
        span = (window.end - window.start).total_seconds()
        return window.start + timedelta(seconds=rng.uniform(0.0, span))
    return _business_hour_timestamp(rng)


def _sample_amount_paise(rng: Random) -> int:
    """Three-humped amount distribution: subscriptions, orders, EMIs.

    A single log-normal would blur the humps together, and the humps are what
    give the economist layer something to trade off later - it is never worth
    three retries and an SMS to chase a 99-rupee subscription.
    """
    roll = rng.random()
    if roll < 0.42:  # small recurring subscriptions
        rupees = float(rng.choice((99, 129, 149, 199, 249, 299, 399, 499)))
    elif roll < 0.85:  # mid-size orders
        rupees = min(3000.0, max(500.0, rng.lognormvariate(7.05, 0.55)))
    else:  # EMIs and big-ticket
        rupees = min(60000.0, max(15000.0, rng.lognormvariate(10.15, 0.45)))
    return int(round(rupees * 100))


def _retry_cap(method: Method, is_recurring: bool) -> int:
    """NPCI/NACH caps on mandate debits are far tighter than one-off retries."""
    if method in _AUTO_DEBIT:
        return 3
    if method == "card" and is_recurring:
        return 3
    return 5


def _causal_weights(
    customer: Customer,
    method: Method,
    is_recurring: bool,
    liquidity: float,
    health: float,
    amount_paise: int,
    mandate_expired: bool,
) -> dict[FailureMode, float]:
    """Weight each failure mode by the conditions actually present.

    Every term below is a claim about the world that a later policy can exploit:
    dry accounts decline for funds, sick issuers time out, unwilling payers let
    collect requests and OTP pages expire.
    """
    w = {
        FailureMode.INSUFFICIENT_FUNDS: 0.9,
        FailureMode.BANK_DOWNTIME: 0.30,
        FailureMode.UPI_COLLECT_TIMEOUT: 0.0,
        FailureMode.THREE_DS_DROPOFF: 0.0,
        FailureMode.EXPIRED_MANDATE: 0.0,
        FailureMode.ISSUER_DECLINE_HARD: 0.20,
        FailureMode.RISK_DECLINED: 0.15,
        FailureMode.TECHNICAL_ERROR: 0.55,
    }

    # Dry account, auto-debit rail -> the classic soft decline.
    funds_pressure = (1.0 - liquidity) ** 1.6
    w[FailureMode.INSUFFICIENT_FUNDS] += 8.5 * funds_pressure * _FUNDS_SENSITIVITY[method]

    # Sick issuer -> everything on that rail fails the same way at once.
    w[FailureMode.BANK_DOWNTIME] += 30.0 * health**1.15
    w[FailureMode.TECHNICAL_ERROR] += 2.0 * health

    unwillingness = 1.0 - customer.intent_to_pay
    if method == "upi_collect":
        # The payer simply never opened the app.
        w[FailureMode.UPI_COLLECT_TIMEOUT] = 1.6 + 8.5 * unwillingness
    if method == "card" and not is_recurring:
        # Only customer-present card payments carry a 3DS challenge to abandon.
        w[FailureMode.THREE_DS_DROPOFF] = 1.2 + 7.5 * unwillingness

    if mandate_expired:
        # Overwhelming: the debit is rejected before it ever reaches the account.
        w[FailureMode.EXPIRED_MANDATE] = 70.0

    if customer.hard_blocked:
        # Dead instrument. Nothing else gets a chance to be the cause.
        w[FailureMode.ISSUER_DECLINE_HARD] = 55.0
        for mode in (
            FailureMode.INSUFFICIENT_FUNDS,
            FailureMode.BANK_DOWNTIME,
            FailureMode.UPI_COLLECT_TIMEOUT,
            FailureMode.THREE_DS_DROPOFF,
            FailureMode.TECHNICAL_ERROR,
        ):
            w[mode] *= 0.10

    # Risk engines fire on ticket size far more than on anything else observable.
    w[FailureMode.RISK_DECLINED] += 1.9 * min(1.0, amount_paise / 3_000_000)

    return w


def _causal_failure_mode(rng: Random, weights: dict[FailureMode, float]) -> FailureMode:
    modes = list(weights)
    return rng.choices(modes, weights=[weights[m] for m in modes], k=1)[0]


def generate_transactions(
    rng: Random, world: World, customers: dict[str, Customer], n: int = 800
) -> list[Transaction]:
    """Generate `n` failed transactions, sorted by time.

    Sorting is not cosmetic: downstream reporting and any future replay harness
    want the feed in the order an operations team would have seen it.
    """
    ids = list(customers)
    txns: list[Transaction] = []

    for i in range(n):
        # Payment volume is concentrated in a minority of customers, so failures
        # repeat on the same ids - which is also why splits must be by customer.
        customer = customers[rng.choices(ids, weights=[1.0 + (j % 7) for j in range(len(ids))], k=1)[0]]

        issuer = _pick_issuer(rng)
        created_at = _sample_created_at(rng, world, issuer)
        health = world.issuer_health(issuer, created_at)

        # The customer's habitual rail wins most of the time; the rest is spread
        # so every method is represented on every kind of customer.
        method: Method = (
            customer.preferred_method
            if rng.random() < 0.55
            else rng.choices(
                list(_FUNDS_SENSITIVITY),
                weights=[0.16, 0.24, 0.24, 0.08, 0.19, 0.09],
                k=1,
            )[0]
        )

        if method in _AUTO_DEBIT:
            is_recurring = True
        elif method == "card":
            is_recurring = rng.random() < 0.42
        elif method == "netbanking":
            is_recurring = rng.random() < 0.10
        else:
            is_recurring = rng.random() < 0.18

        mandate_id: str | None = None
        mandate_expiry: datetime | None = None
        mandate_expired = False
        if method in _MANDATE_METHODS:
            mandate_id = f"mnd_{customer.customer_id[-4:]}{i:04d}"
            if rng.random() < 0.16:
                # Already lapsed at attempt time -> guaranteed EXPIRED_MANDATE.
                mandate_expiry = created_at - timedelta(days=rng.randint(1, 90))
                mandate_expired = True
            else:
                mandate_expiry = created_at + timedelta(days=rng.randint(20, 500))

        amount_paise = _sample_amount_paise(rng)
        liquidity = customer.liquidity(created_at)
        mode = _causal_failure_mode(
            rng,
            _causal_weights(
                customer, method, is_recurring, liquidity, health, amount_paise, mandate_expired
            ),
        )
        spec = SPECS[mode]

        cap = _retry_cap(method, is_recurring)
        # A tail of transactions arrives with the retry budget already spent -
        # those are cases where retrying is not merely unwise but unavailable.
        used = rng.choices(range(cap + 1), weights=[10, 6, 3] + [1.4] * (cap - 2), k=1)[0]

        txns.append(
            Transaction(
                txn_id=f"pay_{i:05d}",
                customer_id=customer.customer_id,
                created_at=created_at,
                amount_paise=amount_paise,
                method=method,
                issuer=issuer,
                is_recurring=is_recurring,
                mandate_id=mandate_id,
                mandate_expiry=mandate_expiry,
                retry_attempts_used=used,
                retry_cap=cap,
                failure_mode=mode.name,
                failure_code=spec.code,
                gateway_message=gateway_message(mode, rng),
                decline_type=spec.decline_type,
                issuer_health_at_failure=health,
            )
        )

    txns.sort(key=lambda t: (t.created_at, t.txn_id))
    return txns
