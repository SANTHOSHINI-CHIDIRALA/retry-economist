"""Three deterministic signals, computed from the observed feed and nothing else.

The point of computing these outside the model is that they are the parts that
must be RIGHT rather than plausible. A language model asked to eyeball whether an
issuer is currently degraded will produce a confident sentence either way; a
rolling-window count either shows a spike or it does not. So the arithmetic is
done here, deterministically and testably, and the model is handed the answer.

Every signal reports a confidence, and the confidences are meant to be honest
rather than flattering - a customer with two prior failures cannot support a
salary-day inference, and the signal says so instead of guessing crisply.

What this module may NOT do:

- read `generator/world.py`, which holds the true downtime windows. Issuer health
  is inferred from failure volume in the feed, which is what a PSP without a
  downtime API actually has;
- read latent customer state. `salary_day` is latent, so the salary cycle is
  inferred from the day-of-month pattern of the customer's own failures;
- read counterfactual outcomes at all.
"""

from __future__ import annotations

import bisect
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Sequence

from retry_economist.policies.base import ObservedTransaction

# ---------------------------------------------------------------------------
# canonical causes
# ---------------------------------------------------------------------------

FUNDS = "insufficient_funds"
DOWNTIME = "bank_downtime"
COLLECT_TIMEOUT = "upi_collect_timeout"
THREE_DS = "three_ds_dropoff"
EXPIRED_MANDATE = "expired_mandate"
HARD_BLOCK = "issuer_decline_hard"
RISK = "risk_declined"
TECHNICAL = "technical_error"
UNKNOWN = "unknown"

CANONICAL_CAUSES: tuple[str, ...] = (
    FUNDS,
    DOWNTIME,
    COLLECT_TIMEOUT,
    THREE_DS,
    EXPIRED_MANDATE,
    HARD_BLOCK,
    RISK,
    TECHNICAL,
    UNKNOWN,
)

#: The acquirer's decline codes, as a payments engineer would document them.
CODE_TO_CAUSE: dict[str, str] = {
    "51": FUNDS,
    "91": DOWNTIME,
    "U69": COLLECT_TIMEOUT,
    "ACS_TIMEOUT": THREE_DS,
    "MANDATE_EXPIRED_M06": EXPIRED_MANDATE,
    "41": HARD_BLOCK,
    "R05": RISK,
    "96": TECHNICAL,
}

#: Free-text patterns over the raw gateway message. Real acquirer feeds are
#: inconsistent in casing, abbreviation and vocabulary, so this reads the message
#: independently of the code - which is the only way to notice when the two
#: disagree.
MESSAGE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"insufficient|low bal|not sufficient|bal too low|no funds", re.I), FUNDS),
    (re.compile(r"not responding|inoperative|unavailable|rb not available|upstream timeout", re.I), DOWNTIME),
    (re.compile(r"collect|payer did not respond|no action from payer", re.I), COLLECT_TIMEOUT),
    (re.compile(r"3ds|otp|acs|authentication", re.I), THREE_DS),
    (re.compile(r"mandate|umrn|re-registration", re.I), EXPIRED_MANDATE),
    # Risk is checked BEFORE the generic block patterns: a risk decline often
    # says "blocked" too, and matching that first would misread every fraud
    # decline as a dead instrument - and report the mismatch as a conflict.
    (re.compile(r"fraud|risk|velocity", re.I), RISK),
    (re.compile(r"lost|stolen|pick up card|blocked|account closed|do not honour", re.I), HARD_BLOCK),
    (re.compile(r"malfunction|system|internal svc|gateway_error|pg-\d+", re.I), TECHNICAL),
)

#: Days of the month Indian payrolls actually cluster on. A domain prior, not a
#: lookup of the generator's parameters - customers paid on other days are
#: inferred imperfectly, and the confidence is expected to reflect that.
CANDIDATE_SALARY_DAYS: tuple[int, ...] = (1, 7, 15, 30)

#: Half-width of the issuer window. Six hours total is long enough to contain a
#: real outage and short enough that a quiet afternoon does not look like one.
ISSUER_WINDOW_HOURS = 3.0


@dataclass(frozen=True, slots=True)
class Signal:
    """One computed signal: a value, an honest confidence, and its workings."""

    name: str
    value: Any
    confidence: float
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "confidence": round(self.confidence, 4),
            "summary": self.summary,
            **self.detail,
        }


@dataclass(frozen=True, slots=True)
class Signals:
    root_cause: Signal
    issuer_health_now: Signal
    liquidity_timing: Signal

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_cause": self.root_cause.to_dict(),
            "issuer_health_now": self.issuer_health_now.to_dict(),
            "liquidity_timing": self.liquidity_timing.to_dict(),
        }


# ---------------------------------------------------------------------------
# (a) root cause
# ---------------------------------------------------------------------------


def message_cause(gateway_message: str) -> str | None:
    """Read the free-text message on its own terms, ignoring the code."""
    for pattern, cause in MESSAGE_PATTERNS:
        if pattern.search(gateway_message):
            return cause
    return None


def root_cause_signal(txn: ObservedTransaction) -> Signal:
    """Normalise code and message into one canonical cause.

    The two fields are read independently and then compared. Agreement is
    strong evidence; disagreement is reported at low confidence rather than
    silently resolved, because a mismatch between the structured code and the
    text an acquirer actually sent is exactly the case where a single confident
    answer would be wrong.
    """
    from_code = CODE_TO_CAUSE.get(txn.failure_code)
    from_message = message_cause(txn.gateway_message)
    detail = {
        "failure_code": txn.failure_code,
        "cause_from_code": from_code,
        "cause_from_message": from_message,
        "gateway_message": txn.gateway_message,
        "agreement": bool(from_code and from_message and from_code == from_message),
    }

    if from_code and from_message and from_code == from_message:
        return Signal(
            "root_cause",
            from_code,
            0.95,
            f"code {txn.failure_code} and the gateway text both read as {from_code}",
            detail,
        )
    if from_code and from_message and from_code != from_message:
        # The code is the more authoritative field, but the disagreement is the
        # headline: a downstream decision should not be taken crisply here.
        return Signal(
            "root_cause",
            from_code,
            0.30,
            (
                f"CONFLICT: code {txn.failure_code} reads as {from_code} but the gateway "
                f"text reads as {from_message}; going with the code, at low confidence"
            ),
            detail,
        )
    if from_code:
        return Signal(
            "root_cause",
            from_code,
            0.80,
            f"code {txn.failure_code} reads as {from_code}; message adds nothing",
            detail,
        )
    if from_message:
        return Signal(
            "root_cause",
            from_message,
            0.55,
            f"code {txn.failure_code} is unrecognised; gateway text reads as {from_message}",
            detail,
        )
    return Signal(
        "root_cause",
        UNKNOWN,
        0.15,
        f"neither code {txn.failure_code} nor the gateway text is recognised",
        detail,
    )


# ---------------------------------------------------------------------------
# the index
# ---------------------------------------------------------------------------


class SignalIndex:
    """Precomputed views over the observed feed, so each signal is a lookup.

    Built once per run from the transactions a policy is allowed to see. It
    holds no counterfactual data and no latent state; everything here could be
    computed by a PSP from its own failure log.
    """

    __slots__ = (
        "_issuer_times",
        "_issuer_baseline_per_hour",
        "_customer_days",
        "_span_hours",
        "n",
    )

    def __init__(self, transactions: Sequence[ObservedTransaction]) -> None:
        self.n = len(transactions)
        issuer_times: dict[str, list[float]] = defaultdict(list)
        customer_days: dict[str, list[int]] = defaultdict(list)

        for txn in transactions:
            issuer_times[txn.issuer].append(txn.created_at.timestamp())
            customer_days[txn.customer_id].append(txn.created_at.day)

        for times in issuer_times.values():
            times.sort()
        self._issuer_times = dict(issuer_times)
        self._customer_days = dict(customer_days)

        stamps = [t for times in self._issuer_times.values() for t in times]
        span = (max(stamps) - min(stamps)) / 3600.0 if len(stamps) > 1 else 0.0
        self._span_hours = span
        # Baseline failure volume per hour, per issuer, across the whole feed.
        self._issuer_baseline_per_hour = {
            issuer: (len(times) / span if span > 0 else 0.0)
            for issuer, times in self._issuer_times.items()
        }

    # -- (b) issuer health ---------------------------------------------------

    def issuer_health_signal(self, txn: ObservedTransaction) -> Signal:
        """Failure-volume spike for this issuer around this timestamp.

        Note what this measures and what it does not. The feed contains only
        FAILED payments, so a true failure *rate* is not computable from it - the
        denominator, successful payments, is not in the data a recovery system
        sees. What is computable, and what actually moves during an incident, is
        failure VOLUME: when an issuer goes down, its failures arrive in a burst.
        So the signal is this issuer's failures per hour in a window around the
        transaction, expressed as a multiple of its own all-feed baseline.

        Deliberately derived rather than read from the simulator's downtime
        windows: a real PSP without an issuer status feed has exactly this and
        nothing more.
        """
        times = self._issuer_times.get(txn.issuer, [])
        baseline = self._issuer_baseline_per_hour.get(txn.issuer, 0.0)
        centre = txn.created_at.timestamp()
        half = ISSUER_WINDOW_HOURS * 3600.0
        lo = bisect.bisect_left(times, centre - half)
        hi = bisect.bisect_right(times, centre + half)
        in_window = hi - lo
        window_hours = 2 * ISSUER_WINDOW_HOURS
        observed_rate = in_window / window_hours

        if baseline <= 0 or len(times) < 5:
            return Signal(
                "issuer_health_now",
                "unknown",
                0.1,
                f"too little history for {txn.issuer} to establish a baseline",
                {
                    "issuer": txn.issuer,
                    "window_hours": window_hours,
                    "failures_in_window": in_window,
                    "multiple_over_baseline": None,
                },
            )

        multiple = observed_rate / baseline
        # Confidence grows with the number of failures actually in the window;
        # a 3x multiple built from two events is not evidence of anything.
        confidence = min(0.9, 0.2 + 0.1 * in_window)
        if multiple >= 3.0:
            state, verdict = "degraded", "clustered failures consistent with a live incident"
        elif multiple >= 1.6:
            state, verdict = "elevated", "failure volume above this issuer's normal"
        else:
            state, verdict = "normal", "failure volume within this issuer's normal range"

        return Signal(
            "issuer_health_now",
            state,
            confidence,
            (
                f"{txn.issuer} shows {multiple:.1f}x its baseline failure volume in the "
                f"{window_hours:.0f}h window around this attempt ({in_window} failures): {verdict}"
            ),
            {
                "issuer": txn.issuer,
                "window_hours": window_hours,
                "failures_in_window": in_window,
                "baseline_per_hour": round(baseline, 4),
                "observed_per_hour": round(observed_rate, 4),
                "multiple_over_baseline": round(multiple, 3),
            },
        )

    # -- (c) liquidity timing -------------------------------------------------

    def liquidity_signal(self, txn: ObservedTransaction) -> Signal:
        """Infer the customer's pay cycle from when their payments fail.

        `salary_day` is latent, so it has to be inferred. The inference rests on
        one observation: accounts run dry at the END of a pay cycle, so a
        customer's failures cluster in the days BEFORE their credit lands. The
        candidate payday that best explains the observed failure days - the one
        that leaves the smallest average gap between a failure and the next
        credit - is the best estimate.

        With one or two prior failures this is close to guessing, and the
        confidence says so. An irregular earner has no cycle to find at all, and
        shows up as a weak margin between candidates.
        """
        days = list(self._customer_days.get(txn.customer_id, []))
        # This transaction's own day is evidence too, but the count of OTHER
        # failures is what the confidence should rest on.
        others = len(days) - 1

        scores: dict[int, float] = {}
        for candidate in CANDIDATE_SALARY_DAYS:
            gaps = [_days_until_day_of_month(day, candidate) for day in days]
            scores[candidate] = sum(gaps) / len(gaps) if gaps else 99.0

        best = min(scores, key=lambda d: scores[d])
        ranked = sorted(scores.values())
        margin = (ranked[1] - ranked[0]) if len(ranked) > 1 else 0.0

        estimated = _days_until_day_of_month(txn.created_at.day, best)

        if others <= 0:
            confidence = 0.15
            basis = "no other failures for this customer; falling back to a population prior"
        else:
            # Evidence grows with prior failures and with how clearly one
            # candidate beats the next.
            confidence = min(0.85, 0.12 + 0.10 * others) * (0.55 + 0.45 * min(1.0, margin / 4.0))
            basis = f"inferred from {others} other failure(s) by this customer"

        return Signal(
            "liquidity_timing",
            best,
            round(confidence, 4),
            (
                f"likely credit around day {best} of the month, about {estimated} day(s) away; "
                f"{basis}"
            ),
            {
                "inferred_salary_day": best,
                "estimated_days_until_likely_credit": estimated,
                "prior_failures_observed": others,
                "candidate_margin_days": round(margin, 3),
                "failure_days_of_month": sorted(days),
            },
        )

    def signals_for(self, txn: ObservedTransaction) -> Signals:
        return Signals(
            root_cause=root_cause_signal(txn),
            issuer_health_now=self.issuer_health_signal(txn),
            liquidity_timing=self.liquidity_signal(txn),
        )


def _days_until_day_of_month(from_day: int, target_day: int) -> int:
    """Days from `from_day` to the next occurrence of `target_day`.

    Uses a 30-day month. The exact month length matters less than the ordering,
    and pretending to a precision the inference does not have would be false.
    """
    delta = target_day - from_day
    return delta if delta > 0 else delta + 30


def _month_end(ts: datetime) -> datetime:  # pragma: no cover - helper kept for clarity
    return (ts.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
