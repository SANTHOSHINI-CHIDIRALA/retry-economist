"""The oracle: what would have happened under every action, for every failure.

This is the file the whole benchmark rests on, for one reason: `do_nothing` has
a real, non-trivial recovery probability. Roughly a fifth to a quarter of failed
payments recover with no intervention at all, because the customer notices and
pays. Any evaluation that ignores that counts those recoveries as wins for
whatever action happened to be taken, and every retry strategy looks brilliant.
Publishing `would_pay_anyway` per transaction turns recovery-rate comparisons
into uplift comparisons, which is the only honest way to score a recovery agent.

Determinism: each (transaction, action) pair gets its own RNG seeded from the
run seed and those two names. Nothing depends on iteration order, so outcomes
are stable even if the transaction list is reordered or the loop is parallelised
later.

Common random numbers: all nine actions for a transaction are judged against one
shared uniform draw, so a difference between two arms is a difference in the
model, never a difference in coin flips. Without this, `max over actions` is
partly a max over noise and the oracle-best policy looks far stronger than it is.
It also preserves the case that matters most for uplift - a customer who would
have paid on their own, interrupted by an action with worse odds than silence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from random import Random
from typing import Any

from retry_economist.generator.customers import Customer, clamp01, next_salary_credit
from retry_economist.generator.failures import SPECS, FailureMode
from retry_economist.generator.world import World
from retry_economist.schema import (
    ACTIONS,
    RECOVERY_HORIZON_HOURS,
    Action,
    ActionOutcome,
    Method,
    OracleRecord,
    Transaction,
    iso,
)

#: Global dial on organic recovery, calibrated so the overall `do_nothing` rate
#: lands in the 18-28% band that matches published dunning benchmarks.
ORGANIC_SCALE = 0.62

#: Beyond two days, waiting stops being free: orders get cancelled, carts are
#: abandoned, subscriptions lapse, and the customer stops recognising the charge.
#: Without this decay, "wait until payday" would dominate every failure mode
#: including outages, and the timing decision would collapse to a single answer.
#: Half-life is set against a ~3-week dunning window: long enough that waiting
#: for a payday two weeks out is still a live option for a subscription debit,
#: short enough that it loses to a 24-hour retry when the block was an outage.
STALENESS_FREE_HOURS = 72.0
STALENESS_HALF_LIFE_HOURS = 504.0

#: Above this, contact fatigue starts eating into success: annoyed customers
#: ignore links, mute the sender, and in the worst case churn deliberately.
ANNOYANCE_THRESHOLD = 0.55

#: Annoyance added by each action, before the failure surcharge below. Silence
#: is free; phoning someone is not.
_ANNOYANCE_COST: dict[Action, float] = {
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

#: A contact that ends in another failure annoys roughly twice as much as one
#: that ends in a paid invoice.
_FAILURE_SURCHARGE: dict[Action, float] = {
    "nudge_then_retry": 0.07,
    "switch_to_upi_intent": 0.05,
    "request_new_mandate": 0.09,
    "escalate_to_human": 0.06,
}


def _staleness(delay_hours: float) -> float:
    """Survival of the invoice itself after waiting `delay_hours` to act."""
    if delay_hours <= STALENESS_FREE_HOURS:
        return 1.0
    return 0.5 ** ((delay_hours - STALENESS_FREE_HOURS) / STALENESS_HALF_LIFE_HOURS)


def _smoothstep(x: float, lo: float, hi: float) -> float:
    """Soft gate in [0, 1]. Smooth so small timing changes move probability."""
    t = clamp01((x - lo) / (hi - lo))
    return t * t * (3.0 - 2.0 * t)


@dataclass(frozen=True, slots=True)
class _Context:
    txn: Transaction
    customer: Customer
    world: World
    mode: FailureMode
    #: One shared uniform draw per transaction - see the module docstring.
    coin: float

    @property
    def is_hard(self) -> bool:
        return SPECS[self.mode].decline_type == "hard"

    @property
    def needs_consent(self) -> bool:
        return SPECS[self.mode].requires_new_consent


def _annoyance_penalty(annoyance: float) -> float:
    """Multiplier on any customer-facing success once fatigue sets in."""
    if annoyance <= ANNOYANCE_THRESHOLD:
        return 1.0
    return max(0.30, 1.0 - 1.25 * (annoyance - ANNOYANCE_THRESHOLD))


def _attempt_fatigue(txn: Transaction) -> float:
    """Attempts already burnt are evidence the block is persistent, not a blip."""
    return max(0.55, 1.0 - 0.09 * txn.retry_attempts_used)


def _authorisation_probability(
    ctx: _Context,
    at: datetime,
    *,
    method: Method,
    fresh_consent: bool = False,
    bypasses_mandate: bool = False,
    intent: float | None = None,
) -> float:
    """Probability that one authorisation attempt at `at` succeeds.

    Every retry-shaped action funnels through here, so the model of "why does a
    payment work now when it did not work then" exists exactly once: the money
    is there, the bank is up, the consent is valid, and the customer is willing.
    """
    txn, cust, world = ctx.txn, ctx.customer, ctx.world

    if ctx.is_hard:
        # Blocked card, closed account, fraud rule: no amount of retrying, on
        # any rail, at any hour, ever clears these.
        return 0.0
    if ctx.needs_consent and not (fresh_consent or bypasses_mandate):
        return 0.0

    liq = cust.liquidity(at)
    deg = world.issuer_health(txn.issuer, at)
    base = world.issuers[txn.issuer].base_success_rate
    willing = cust.intent_to_pay if intent is None else intent

    # Even a healthy account needs *some* balance for any mode to clear.
    funds_gate = 0.55 + 0.45 * _smoothstep(liq, 0.08, 0.55)

    if ctx.mode is FailureMode.INSUFFICIENT_FUNDS:
        p = base * _smoothstep(liq, 0.18, 0.72) * (1.0 - 0.85 * deg)
    elif ctx.mode is FailureMode.BANK_DOWNTIME:
        p = base * (1.0 - deg) ** 1.6 * funds_gate
    elif ctx.mode is FailureMode.UPI_COLLECT_TIMEOUT:
        p = base * (0.12 + 0.76 * willing) * funds_gate * (1.0 - 0.6 * deg)
        if method == "upi_intent":
            # A push request the payer taps beats a pull request they ignore.
            p *= 1.28
    elif ctx.mode is FailureMode.THREE_DS_DROPOFF:
        p = base * (0.10 + 0.72 * willing) * funds_gate * (1.0 - 0.5 * deg)
        if method == "upi_intent":
            # Sidesteps the OTP page that was abandoned in the first place.
            p *= 1.38
    elif ctx.mode is FailureMode.TECHNICAL_ERROR:
        p = base * 0.88 * (1.0 - deg) * funds_gate
    elif ctx.mode is FailureMode.EXPIRED_MANDATE:
        p = base * (0.35 + 0.50 * willing) * funds_gate
        if bypasses_mandate and not fresh_consent:
            # A one-off collect dodges the dead mandate but needs the customer
            # to act, so it converts worse than re-registering properly.
            p *= 0.72
    else:  # pragma: no cover - hard modes returned above
        p = 0.0

    if method == "upi_intent" and cust.preferred_method == "card":
        # Card-first customers are the least comfortable being pushed to UPI.
        p *= 0.85

    return min(0.97, max(0.0, p * _attempt_fatigue(txn)))


def _liquidity_peak(cust: Customer, start: datetime, hours: int) -> tuple[float, float]:
    """Best liquidity within `hours` of `start`, and when it occurs.

    Sampled every four hours rather than solved analytically because the gig
    curve has no closed form, and four hours is finer than any action's timing
    granularity anyway.
    """
    best_val, best_h = cust.liquidity(start), 0.0
    for h in range(4, hours + 1, 4):
        v = cust.liquidity(start + timedelta(hours=h))
        if v > best_val:
            best_val, best_h = v, float(h)
    return best_val, best_h


def _organic_recovery(ctx: _Context, rng: Random) -> ActionOutcome:
    """`do_nothing`: the customer sorts it out themselves, or does not.

    This is the ground-truth "would have paid anyway" label. It is driven by
    intent plus the liquidity the customer comes into over the next 72 hours,
    so it correlates with the same latent state the actions exploit - which is
    exactly what makes uplift hard and worth measuring.
    """
    cust, txn = ctx.customer, ctx.txn
    if ctx.is_hard:
        # The instrument is dead; self-service cannot resurrect it.
        return ActionOutcome(False, None, 0, 0.0)

    now_liq = cust.liquidity(txn.created_at)
    peak, peak_h = _liquidity_peak(cust, txn.created_at, 72)
    gain = max(0.0, peak - now_liq)

    p = ORGANIC_SCALE * (0.05 + 0.42 * cust.intent_to_pay + 0.50 * gain + 0.18 * now_liq)
    if ctx.mode in (FailureMode.UPI_COLLECT_TIMEOUT, FailureMode.THREE_DS_DROPOFF):
        # They know they walked away from a payment; some of them come back.
        p *= 1.12
    if ctx.needs_consent:
        # Re-registering a mandate unprompted is rare but does happen.
        p *= 0.38
    p *= _annoyance_penalty(cust.annoyance)
    p = min(0.85, p)

    if ctx.coin >= p:
        return ActionOutcome(False, None, 0, 0.0)

    # Self-serve payments cluster around the moment money lands, not uniformly.
    hours = peak_h + rng.uniform(0.5, 14.0) if peak_h > 0 else rng.uniform(2.0, 60.0)
    return ActionOutcome(True, min(72.0, hours), 0, 0.0)


def _delayed_retry(
    ctx: _Context, rng: Random, delay_hours: float, action: Action
) -> ActionOutcome:
    """A plain automated retry `delay_hours` after the failure."""
    if ctx.txn.attempts_left <= 0:
        # The rail is out of retries: the action is simply unavailable, and an
        # agent that picks it has wasted its turn.
        return ActionOutcome(False, None, 0, 0.0)
    at = ctx.txn.created_at + timedelta(hours=delay_hours)
    p = _authorisation_probability(ctx, at, method=ctx.txn.method)
    p *= _annoyance_penalty(ctx.customer.annoyance) * _staleness(delay_hours)
    recovered = ctx.coin < p
    cost = _ANNOYANCE_COST[action]
    hours = delay_hours + rng.uniform(0.02, 0.4) if recovered else None
    return ActionOutcome(recovered, hours, 1, cost)


def _salary_day_delay(ctx: _Context, rng: Random) -> float:
    """Hours to wait to land just after the next credit into the account.

    Gig workers have no payday to aim at, so the action degrades to a long,
    liquidity-blind wait. That degradation is deliberate: a router that fires
    `retry_next_salary_day` at an irregular earner should not be rewarded.
    """
    cust = ctx.customer
    payday = next_salary_credit(cust, ctx.txn.created_at)
    if payday is None:
        return 72.0 + rng.uniform(0.0, 6.0)
    # A couple of hours after the credit clears, not at the same instant.
    delay = (payday - ctx.txn.created_at).total_seconds() / 3600.0 + rng.uniform(2.0, 9.0)
    return max(1.0, delay)


def _nudge_then_retry(ctx: _Context, rng: Random) -> ActionOutcome:
    """Send a reminder / payment link, then retry about six hours later.

    Genuinely lifts willingness, which is why it works on collect timeouts and
    OTP dropoffs. It does nothing whatsoever about an empty bank account - the
    reminder just makes the customer feel worse about a payment they cannot make.
    """
    cust, txn = ctx.customer, ctx.txn
    delay = 6.0 + rng.uniform(0.0, 3.0)
    at = txn.created_at + timedelta(hours=delay)
    lifted = cust.intent_to_pay + 0.40 * (1.0 - cust.intent_to_pay)

    if ctx.is_hard:
        p = 0.0
    elif ctx.mode is FailureMode.EXPIRED_MANDATE:
        # Pay-by-link sidesteps the dead mandate for this one invoice, but does
        # not restore the recurring rail - `request_new_mandate` still wins.
        p = _authorisation_probability(ctx, at, method="upi_intent", bypasses_mandate=True)
    elif ctx.mode in (FailureMode.UPI_COLLECT_TIMEOUT, FailureMode.THREE_DS_DROPOFF):
        p = _authorisation_probability(ctx, at, method=txn.method, intent=lifted)
    else:
        # Same odds as any other six-hour wait, but with an annoyance bill.
        p = _authorisation_probability(ctx, at, method=txn.method)

    if txn.attempts_left <= 0 and ctx.mode is not FailureMode.EXPIRED_MANDATE:
        # No automated attempt left, so only the link itself can convert.
        p *= 0.45

    p *= _annoyance_penalty(cust.annoyance) * _staleness(delay)
    recovered = ctx.coin < p
    attempts = 1 if txn.attempts_left > 0 else 0
    cost = _ANNOYANCE_COST["nudge_then_retry"] + (
        0.0 if recovered else _FAILURE_SURCHARGE["nudge_then_retry"]
    )
    return ActionOutcome(recovered, delay + rng.uniform(0.1, 1.5) if recovered else None, attempts, cost)


def _switch_to_upi_intent(ctx: _Context, rng: Random) -> ActionOutcome:
    """Move the customer onto a UPI push request on a fresh instrument.

    Changes the rail, not the bank: an issuer outage or an empty account follows
    the customer across. What it does fix is consent (a dead mandate is bypassed
    entirely) and friction (no OTP page to abandon).
    """
    delay = 0.5 + rng.uniform(0.0, 1.5)
    at = ctx.txn.created_at + timedelta(hours=delay)
    p = _authorisation_probability(ctx, at, method="upi_intent", bypasses_mandate=True)
    p *= _annoyance_penalty(ctx.customer.annoyance) * _staleness(delay)
    recovered = ctx.coin < p
    cost = _ANNOYANCE_COST["switch_to_upi_intent"] + (
        0.0 if recovered else _FAILURE_SURCHARGE["switch_to_upi_intent"]
    )
    return ActionOutcome(recovered, delay + rng.uniform(0.1, 2.0) if recovered else None, 1, cost)


def _request_new_mandate(ctx: _Context, rng: Random) -> ActionOutcome:
    """Ask the customer to re-authorise: new mandate, or a new instrument.

    Slow and irritating, and the only real cure for expired consent. It is also
    one of just two actions with any chance against a hard decline, since a new
    instrument is precisely what a blocked card needs.
    """
    cust, txn = ctx.customer, ctx.txn
    delay = 24.0 + rng.uniform(0.0, 48.0)
    at = txn.created_at + timedelta(hours=delay)

    if ctx.mode is FailureMode.ISSUER_DECLINE_HARD:
        # Re-registration on a working instrument - only if they bother.
        p = 0.18 + 0.34 * cust.intent_to_pay
    elif ctx.mode is FailureMode.RISK_DECLINED:
        # A new mandate does not placate a risk engine that has already fired.
        p = 0.06 + 0.12 * cust.intent_to_pay
    else:
        p = _authorisation_probability(ctx, at, method=txn.method, fresh_consent=True)
        if not ctx.needs_consent:
            # Overkill for a soft decline: heavy friction for no extra cure.
            p *= 0.72

    p *= _annoyance_penalty(cust.annoyance) * _staleness(delay)
    recovered = ctx.coin < p
    cost = _ANNOYANCE_COST["request_new_mandate"] + (
        0.0 if recovered else _FAILURE_SURCHARGE["request_new_mandate"]
    )
    hours = delay + rng.uniform(0.5, 8.0) if recovered else None
    return ActionOutcome(recovered, hours, 1, cost)


def _escalate_to_human(ctx: _Context, rng: Random) -> ActionOutcome:
    """Put a person on it. Works on almost anything, at a price paid later.

    Consumes no automated retry attempt because the agent takes the case off the
    rail entirely - the cost of this action is money and time, and the economist
    layer is where that gets weighed.
    """
    cust = ctx.customer
    delay = 12.0 + rng.uniform(0.0, 48.0)
    p = 0.20 + 0.40 * cust.intent_to_pay
    if ctx.is_hard:
        p *= 0.55  # a human can re-onboard a dead instrument, sometimes
    elif ctx.mode is FailureMode.INSUFFICIENT_FUNDS:
        # An agent can agree a date the customer can meet - but talking to
        # someone does not create money, so this stays below simply waiting
        # for the salary that is already on its way.
        p *= 0.40 + 0.40 * _smoothstep(
            _liquidity_peak(cust, ctx.txn.created_at, 96)[0], 0.2, 0.7
        )
    p *= _annoyance_penalty(cust.annoyance) * _staleness(delay)
    recovered = ctx.coin < min(0.9, p)
    cost = _ANNOYANCE_COST["escalate_to_human"] + (
        0.0 if recovered else _FAILURE_SURCHARGE["escalate_to_human"]
    )
    hours = delay + rng.uniform(0.5, 12.0) if recovered else None
    return ActionOutcome(recovered, hours, 0, cost)


def simulate_action(ctx: _Context, action: Action, rng: Random) -> ActionOutcome:
    """Dispatch one action, then apply the horizon rule uniformly."""
    if action == "do_nothing":
        outcome = _organic_recovery(ctx, rng)
    elif action == "retry_now":
        outcome = _delayed_retry(ctx, rng, 0.08, action)
    elif action == "retry_in_2h":
        outcome = _delayed_retry(ctx, rng, 2.0, action)
    elif action == "retry_in_24h":
        outcome = _delayed_retry(ctx, rng, 24.0, action)
    elif action == "retry_next_salary_day":
        outcome = _delayed_retry(ctx, rng, _salary_day_delay(ctx, rng), action)
    elif action == "nudge_then_retry":
        outcome = _nudge_then_retry(ctx, rng)
    elif action == "switch_to_upi_intent":
        outcome = _switch_to_upi_intent(ctx, rng)
    elif action == "request_new_mandate":
        outcome = _request_new_mandate(ctx, rng)
    elif action == "escalate_to_human":
        outcome = _escalate_to_human(ctx, rng)
    else:  # pragma: no cover - ACTIONS is closed
        raise ValueError(f"unknown action: {action}")

    if outcome.recovered and (outcome.hours_to_recovery or 0.0) > RECOVERY_HORIZON_HOURS:
        # Money that arrives after the horizon is not recovery, it is churn with
        # a late payment attached. Keeping the annoyance cost is intentional.
        return ActionOutcome(False, None, outcome.attempts_consumed, outcome.customer_annoyance_delta)
    return outcome


def _latent_snapshot(ctx: _Context) -> dict[str, Any]:
    """Latent state at failure time, for post-hoc analysis only.

    Written to the oracle so experiments can slice by true cause without
    re-running the simulation; never written to the observed feed.
    """
    cust, txn = ctx.customer, ctx.txn
    peak, peak_h = _liquidity_peak(cust, txn.created_at, 72)
    payday = next_salary_credit(cust, txn.created_at)
    return {
        "salary_day": cust.salary_day,
        "intent_to_pay": cust.intent_to_pay,
        "annoyance": cust.annoyance,
        "hard_blocked": cust.hard_blocked,
        "liquidity_floor": cust.liquidity_floor,
        "liquidity_at_failure": round(cust.liquidity(txn.created_at), 4),
        "liquidity_peak_next_72h": round(peak, 4),
        "hours_to_liquidity_peak": peak_h,
        "next_salary_credit": iso(payday),
        "issuer_reliability_class": ctx.world.issuers[txn.issuer].reliability_class,
        "issuer_health_at_failure": round(txn.issuer_health_at_failure, 4),
        "decline_type": SPECS[ctx.mode].decline_type,
        "requires_new_consent": SPECS[ctx.mode].requires_new_consent,
    }


def build_oracle(
    txns: list[Transaction], customers: dict[str, Customer], world: World, seed: int
) -> list[OracleRecord]:
    """Evaluate every action against every transaction."""
    records: list[OracleRecord] = []
    for txn in txns:
        ctx = _Context(
            txn,
            customers[txn.customer_id],
            world,
            FailureMode[txn.failure_mode],
            coin=Random(f"{seed}|{txn.txn_id}|coin").random(),
        )
        outcomes = {
            action: simulate_action(ctx, action, Random(f"{seed}|{txn.txn_id}|{action}"))
            for action in ACTIONS
        }
        records.append(
            OracleRecord(
                txn_id=txn.txn_id,
                customer_id=txn.customer_id,
                failure_mode=txn.failure_mode,
                would_pay_anyway=outcomes["do_nothing"].recovered,
                latent=_latent_snapshot(ctx),
                outcomes=outcomes,
            )
        )
    return records
