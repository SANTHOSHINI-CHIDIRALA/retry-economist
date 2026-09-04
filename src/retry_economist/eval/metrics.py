"""Turning outcomes into the numbers that decide whether a policy is any good.

The headline number in payment recovery is recovery rate, and on its own it is
close to useless. A policy that retries everything will beat a careful one on
recovery rate while spending more, irritating more customers, and getting credit
for every payment that was going to arrive regardless.

So attribution splits FIRST on whether the policy actually did anything, and
only then on how it turned out. The earlier four-bucket scheme conflated the two
and mislabelled restraint as failure: a customer left alone who paid unaided
landed in "wasted", and a hopeless invoice left alone landed in "futile", as
though money had been spent on them. Both are the system working exactly as
intended, at zero cost.

    ACTED
      incremental          recovered, would not have paid   <- real revenue
      cannibalised         lost it, would have paid         <- revenue destroyed
      wasted               recovered, would have paid       <- paid for nothing
      futile               lost it, would not have paid     <- paid for nothing

    ABSTAINED
      correct_restraint    left alone, and they paid        <- right call, free
      correct_walkaway     left alone, nothing would work   <- right call, free
      missed_opportunity   left alone, something would work <- the real miss

Seven buckets, mutually exclusive and exhaustive, summing to n by construction.
`restraint_precision` - the share of untouched transactions that no available
action could have improved - is the project's central claim, so it is computed
here as a first-class metric rather than assembled by hand in a report.

Everything is also computed per failure code, because a policy that wins overall
by getting one common failure mode right is a much weaker result than one that
wins across modes, and only the breakdown tells them apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import fmean, median
from typing import Any, Mapping, Sequence

from retry_economist.eval.costs import (
    CUSTOMER_LIFETIME_VALUE_PAISE,
    annoyance_paise_per_unit,
    annoyance_to_paise,
    recovered_value_paise,
)
from retry_economist.eval.simulator import RunResult, TxnOutcome
from retry_economist.policies.base import ObservedTransaction
from retry_economist.schema import ACTIONS, ATTEMPTS_CONSUMED


@dataclass(frozen=True, slots=True)
class Bucket:
    """One attribution bucket, counted two ways.

    `share` weights every transaction equally; `rupee_share` weights it by the
    money at stake. With a median amount around INR 700 and a p95 above INR
    31,000, the two views can rank policies differently - a policy that quietly
    concentrates on small subscriptions looks fine by count and thin by value.
    Both are reported, always; neither replaces the other.
    """

    count: int
    share: float
    paise: int
    rupee_share: float

    @property
    def rupees(self) -> float:
        return self.paise / 100.0

    def to_dict(self) -> dict[str, float]:
        return {
            "count": self.count,
            "share": round(self.share, 6),
            "rupees": round(self.rupees, 2),
            "rupee_share": round(self.rupee_share, 6),
        }


@dataclass(frozen=True, slots=True)
class DecisionQuality:
    """The seven buckets read as a confusion matrix on one binary decision: act.

    Restraint precision on its own is gameable, and gameable by the most trivial
    policy there is - `do_nothing` scores 61.9% on it while recovering nothing it
    did not already get for free. The fix is to report restraint against recall:
    a policy is only being careful if the transactions it declined to touch were
    genuinely not worth touching AND it still caught the ones that were.

        TP  incremental                            acted, and it worked
        FP  wasted + futile + cannibalised          acted, and it should not have
        FN  missed_opportunity                      did not act, and should have
        TN  correct_restraint + correct_walkaway    did not act, correctly

    A transaction the policy acted on that some OTHER action would have
    recovered counts as a false positive, not a false negative: the policy did
    decide to act, it simply chose the wrong action. Misses are reserved for
    decisions not to act at all.
    """

    true_positive: int
    false_positive: int
    false_negative: int
    true_negative: int
    #: None when the policy never acted - a rate over an empty denominator says
    #: nothing, and 0.0 would falsely read as "acted, and was always wrong".
    precision: float | None
    recall: float | None
    f1: float | None

    @property
    def total(self) -> int:
        return self.true_positive + self.false_positive + self.false_negative + self.true_negative

    def to_dict(self) -> dict[str, Any]:
        return {
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "false_negative": self.false_negative,
            "true_negative": self.true_negative,
            "precision": None if self.precision is None else round(self.precision, 6),
            "recall": None if self.recall is None else round(self.recall, 6),
            "f1": None if self.f1 is None else round(self.f1, 6),
        }


def decision_quality(tp: int, fp: int, fn: int, tn: int) -> DecisionQuality:
    """Build precision/recall/F1 from the four cells, guarding empty denominators."""
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    if precision is None or recall is None or (precision + recall) == 0:
        f1 = None
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return DecisionQuality(
        true_positive=tp,
        false_positive=fp,
        false_negative=fn,
        true_negative=tn,
        precision=precision,
        recall=recall,
        f1=f1,
    )


@dataclass(frozen=True, slots=True)
class Metrics:
    """Everything the scoreboard reports for one policy over one population."""

    n: int
    n_customers: int

    # --- was acting the right call? ----------------------------------------
    #
    # The primary view, reported ahead of recovery rate: recovery rate rewards
    # acting indiscriminately, while this asks whether each decision to act or
    # abstain was the correct one.
    decision: DecisionQuality
    #: The same matrix weighted by rupees at stake. Capturing one INR 60,000 EMI
    #: is worth two hundred INR 299 subscriptions, and a count-weighted view
    #: hides which of those a policy is actually catching.
    rupee_decision: DecisionQuality

    recovery_rate: float
    organic_rate: float
    net_uplift_pp: float
    #: How many transactions actually recovered. Carried explicitly so a report
    #: can tell "no recoveries" apart from "no transactions".
    n_recovered: int

    # --- how long the money took to arrive ---------------------------------
    #
    # Recovery rate says nothing about when. `retry_next_salary_day` recovers at
    # roughly 290 hours on average, so a policy can beat another on recovery
    # rate purely by being willing to wait a fortnight - while the merchant
    # carries the receivable, the customer forgets the purchase, and the order
    # may already have been cancelled. The scoreboard exposes that trade rather
    # than concealing it inside a single percentage.
    #: Over recovered transactions only. None when nothing recovered - not 0,
    #: which would read as "recovered instantly".
    mean_days_to_recovery: float | None
    median_days_to_recovery: float | None
    #: Over ALL transactions, so it is directly comparable with recovery_rate
    #: and can never exceed it.
    recovered_within_72h_rate: float

    # --- the same picture weighted by money at stake -----------------------
    #
    # Amounts span two orders of magnitude here (median ~INR 700, p95 ~INR
    # 31,000), so counting transactions and counting rupees are different
    # questions. Reported alongside the count-weighted figures, never instead of
    # them: a divergence between the two views is itself the finding.
    total_rupees_at_risk: float
    rupee_recovery_rate: float
    rupee_organic_rate: float
    rupee_net_uplift_pp: float

    # --- what the policy chose to do -------------------------------------
    n_acted: int
    n_abstained: int
    action_rate: float

    # --- outcomes where it acted -----------------------------------------
    incremental: Bucket
    cannibalised: Bucket
    wasted: Bucket
    futile: Bucket

    # --- outcomes where it abstained --------------------------------------
    correct_restraint: Bucket
    correct_walkaway: Bucket
    missed_opportunity: Bucket
    #: Of the transactions left untouched, the share no available action could
    #: have improved. None when the policy never abstained.
    restraint_precision: float | None

    # --- money -------------------------------------------------------------
    incremental_rupees: float
    cannibalised_rupees: float
    net_rupees: float
    total_cost_rupees: float
    annoyance_units: float
    annoyance_cost_rupees: float
    #: Net revenue less everything it cost to get, including relationship
    #: damage. The only figure that can go negative, and the one that does.
    net_value_rupees: float
    #: None rather than infinity when net revenue is zero or negative: dividing
    #: by a loss produces a number that looks like a cost per rupee and means
    #: nothing at all. The report says "no net revenue" instead of printing it.
    cost_per_incremental_rupee: float | None
    #: The lifetime value this run was priced at, since it is an assumption and
    #: the numbers above move with it.
    clv_paise: int

    total_attempts: int
    attempts_per_txn: float
    contact_rate: float
    #: None when the policy never escalated - a rate over an empty denominator.
    false_escalation_rate: float | None
    compliance_violations: int
    hard_decline_retry_waste: int

    per_failure_code: dict[str, "Metrics"] = field(default_factory=dict)

    def to_dict(self, *, include_breakdown: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "n": self.n,
            "n_customers": self.n_customers,
            "decision_quality": self.decision.to_dict(),
            "rupee_decision_quality": self.rupee_decision.to_dict(),
            "recovery_rate": round(self.recovery_rate, 6),
            "organic_rate": round(self.organic_rate, 6),
            "net_uplift_pp": round(self.net_uplift_pp, 4),
            "timing": {
                "mean_days_to_recovery": (
                    None
                    if self.mean_days_to_recovery is None
                    else round(self.mean_days_to_recovery, 3)
                ),
                "median_days_to_recovery": (
                    None
                    if self.median_days_to_recovery is None
                    else round(self.median_days_to_recovery, 3)
                ),
                "recovered_within_72h_rate": round(self.recovered_within_72h_rate, 6),
                # Explicit, so an empty slice and a slice that recovered nothing
                # stay distinguishable after serialisation - both would
                # otherwise be a bare null.
                "n": self.n,
                "n_recovered": self.n_recovered,
            },
            "rupee_weighted": {
                "total_rupees_at_risk": round(self.total_rupees_at_risk, 2),
                "recovery_rate": round(self.rupee_recovery_rate, 6),
                "organic_rate": round(self.rupee_organic_rate, 6),
                "net_uplift_pp": round(self.rupee_net_uplift_pp, 4),
            },
            "n_acted": self.n_acted,
            "n_abstained": self.n_abstained,
            "action_rate": round(self.action_rate, 6),
            "acted_buckets": {
                "incremental": self.incremental.to_dict(),
                "cannibalised": self.cannibalised.to_dict(),
                "wasted": self.wasted.to_dict(),
                "futile": self.futile.to_dict(),
            },
            "abstained_buckets": {
                "correct_restraint": self.correct_restraint.to_dict(),
                "correct_walkaway": self.correct_walkaway.to_dict(),
                "missed_opportunity": self.missed_opportunity.to_dict(),
            },
            "restraint_precision": (
                None if self.restraint_precision is None else round(self.restraint_precision, 6)
            ),
            "incremental_rupees": round(self.incremental_rupees, 2),
            "cannibalised_rupees": round(self.cannibalised_rupees, 2),
            "net_rupees": round(self.net_rupees, 2),
            "total_cost_rupees": round(self.total_cost_rupees, 2),
            "annoyance_units": round(self.annoyance_units, 4),
            "annoyance_cost_rupees": round(self.annoyance_cost_rupees, 2),
            "net_value_rupees": round(self.net_value_rupees, 2),
            "cost_per_incremental_rupee": (
                None
                if self.cost_per_incremental_rupee is None
                else round(self.cost_per_incremental_rupee, 4)
            ),
            "clv_paise": self.clv_paise,
            "total_attempts": self.total_attempts,
            "attempts_per_txn": round(self.attempts_per_txn, 4),
            "contact_rate": round(self.contact_rate, 6),
            "false_escalation_rate": (
                None if self.false_escalation_rate is None else round(self.false_escalation_rate, 6)
            ),
            "compliance_violations": self.compliance_violations,
            "hard_decline_retry_waste": self.hard_decline_retry_waste,
        }
        if include_breakdown:
            out["per_failure_code"] = {
                code: m.to_dict(include_breakdown=False)
                for code, m in sorted(self.per_failure_code.items())
            }
        return out


def _bucket(members: Sequence[TxnOutcome], n: int, total_paise: int) -> Bucket:
    paise = sum(recovered_value_paise(o.amount_paise) for o in members)
    return Bucket(
        count=len(members),
        share=(len(members) / n if n else 0.0),
        paise=paise,
        rupee_share=(paise / total_paise if total_paise else 0.0),
    )


def _empty(clv_paise: int, violations: int) -> Metrics:
    zero = Bucket(0, 0.0, 0, 0.0)
    return Metrics(
        n=0,
        n_customers=0,
        decision=decision_quality(0, 0, 0, 0),
        rupee_decision=decision_quality(0, 0, 0, 0),
        recovery_rate=0.0,
        n_recovered=0,
        organic_rate=0.0,
        net_uplift_pp=0.0,
        mean_days_to_recovery=None,
        median_days_to_recovery=None,
        recovered_within_72h_rate=0.0,
        total_rupees_at_risk=0.0,
        rupee_recovery_rate=0.0,
        rupee_organic_rate=0.0,
        rupee_net_uplift_pp=0.0,
        n_acted=0,
        n_abstained=0,
        action_rate=0.0,
        incremental=zero,
        cannibalised=zero,
        wasted=zero,
        futile=zero,
        correct_restraint=zero,
        correct_walkaway=zero,
        missed_opportunity=zero,
        restraint_precision=None,
        incremental_rupees=0.0,
        cannibalised_rupees=0.0,
        net_rupees=0.0,
        total_cost_rupees=0.0,
        annoyance_units=0.0,
        annoyance_cost_rupees=0.0,
        net_value_rupees=0.0,
        cost_per_incremental_rupee=None,
        clv_paise=clv_paise,
        total_attempts=0,
        attempts_per_txn=0.0,
        contact_rate=0.0,
        false_escalation_rate=None,
        compliance_violations=violations,
        hard_decline_retry_waste=0,
    )


def compute(
    outcomes: Sequence[TxnOutcome],
    *,
    violations: int = 0,
    breakdown: bool = True,
    clv_paise: int = CUSTOMER_LIFETIME_VALUE_PAISE,
) -> Metrics:
    """Compute every metric over a set of transaction outcomes.

    `clv_paise` prices annoyance. It is passed rather than read from the module
    so the sensitivity sweep can re-price a finished run without re-simulating.
    """
    n = len(outcomes)
    if n == 0:
        return _empty(clv_paise, violations)

    acted = [o for o in outcomes if o.acted]
    abstained = [o for o in outcomes if not o.acted]

    total_paise = sum(recovered_value_paise(o.amount_paise) for o in outcomes)

    incremental = _bucket([o for o in outcomes if o.incremental], n, total_paise)
    cannibalised = _bucket([o for o in outcomes if o.cannibalised], n, total_paise)
    wasted = _bucket([o for o in outcomes if o.wasted], n, total_paise)
    futile = _bucket([o for o in outcomes if o.futile], n, total_paise)
    restraint = _bucket([o for o in outcomes if o.correct_restraint], n, total_paise)
    walkaway = _bucket([o for o in outcomes if o.correct_walkaway], n, total_paise)
    missed = _bucket([o for o in outcomes if o.missed_opportunity], n, total_paise)

    recovery_rate = sum(o.recovered for o in outcomes) / n
    organic_rate = sum(o.would_pay_anyway for o in outcomes) / n

    # Timing, over recovered transactions only. A policy that recovers nothing
    # gets None rather than 0.0: zero days would read as instant success.
    recovered_hours = [
        o.hours_to_recovery for o in outcomes if o.recovered and o.hours_to_recovery is not None
    ]
    within_72h = sum(
        1
        for o in outcomes
        if o.recovered and o.hours_to_recovery is not None and o.hours_to_recovery <= 72.0
    )

    # Rupee weighting: the same headline rates, asking "how much money" instead
    # of "how many invoices".
    recovered_paise = sum(recovered_value_paise(o.amount_paise) for o in outcomes if o.recovered)
    organic_paise = sum(
        recovered_value_paise(o.amount_paise) for o in outcomes if o.would_pay_anyway
    )
    rupee_recovery_rate = recovered_paise / total_paise if total_paise else 0.0
    rupee_organic_rate = organic_paise / total_paise if total_paise else 0.0

    # The act/abstain decision as a confusion matrix, counted and weighted.
    decision = decision_quality(
        tp=incremental.count,
        fp=wasted.count + futile.count + cannibalised.count,
        fn=missed.count,
        tn=restraint.count + walkaway.count,
    )
    rupee_decision = decision_quality(
        tp=incremental.paise,
        fp=wasted.paise + futile.paise + cannibalised.paise,
        fn=missed.paise,
        tn=restraint.paise + walkaway.paise,
    )

    net_rupees = incremental.rupees - cannibalised.rupees
    total_cost_paise = sum(o.total_cost_paise for o in outcomes)
    annoyance_units = sum(o.annoyance_delta for o in outcomes)
    annoyance_paise = annoyance_to_paise(annoyance_units, clv_paise=clv_paise)

    # Cost per rupee of net new revenue. Both sides in paise so the ratio is
    # exact; a merchant reads this as "I spend this much to earn a rupee".
    net_paise = net_rupees * 100.0
    cost_per_incremental_rupee = (
        (total_cost_paise + annoyance_paise) / net_paise if net_paise > 0 else None
    )

    escalated = [o for o in outcomes if "escalate_to_human" in o.actions_executed]
    false_escalation_rate = (
        sum(o.would_pay_anyway for o in escalated) / len(escalated) if escalated else None
    )

    # Debit attempts spent on declines that can never clear. Not a proxy for
    # waste - it is waste, by definition, and it is the cheapest thing for a
    # policy to stop doing.
    hard_waste = sum(o.attempts_used for o in outcomes if o.decline_type == "hard")

    per_code: dict[str, Metrics] = {}
    if breakdown:
        for code in sorted({o.failure_code for o in outcomes}):
            subset = [o for o in outcomes if o.failure_code == code]
            per_code[code] = compute(
                subset,
                violations=sum(o.compliance_violation for o in subset),
                breakdown=False,
                clv_paise=clv_paise,
            )

    return Metrics(
        n=n,
        n_customers=len({o.customer_id for o in outcomes}),
        decision=decision,
        rupee_decision=rupee_decision,
        recovery_rate=recovery_rate,
        n_recovered=sum(o.recovered for o in outcomes),
        organic_rate=organic_rate,
        net_uplift_pp=(recovery_rate - organic_rate) * 100.0,
        mean_days_to_recovery=(fmean(recovered_hours) / 24.0 if recovered_hours else None),
        median_days_to_recovery=(median(recovered_hours) / 24.0 if recovered_hours else None),
        recovered_within_72h_rate=within_72h / n,
        total_rupees_at_risk=total_paise / 100.0,
        rupee_recovery_rate=rupee_recovery_rate,
        rupee_organic_rate=rupee_organic_rate,
        rupee_net_uplift_pp=(rupee_recovery_rate - rupee_organic_rate) * 100.0,
        n_acted=len(acted),
        n_abstained=len(abstained),
        action_rate=len(acted) / n,
        incremental=incremental,
        cannibalised=cannibalised,
        wasted=wasted,
        futile=futile,
        correct_restraint=restraint,
        correct_walkaway=walkaway,
        missed_opportunity=missed,
        restraint_precision=(
            (restraint.count + walkaway.count) / len(abstained) if abstained else None
        ),
        incremental_rupees=incremental.rupees,
        cannibalised_rupees=cannibalised.rupees,
        net_rupees=net_rupees,
        total_cost_rupees=total_cost_paise / 100.0,
        annoyance_units=annoyance_units,
        annoyance_cost_rupees=annoyance_paise / 100.0,
        net_value_rupees=net_rupees - (total_cost_paise + annoyance_paise) / 100.0,
        cost_per_incremental_rupee=cost_per_incremental_rupee,
        clv_paise=clv_paise,
        total_attempts=sum(o.attempts_used for o in outcomes),
        attempts_per_txn=sum(o.attempts_used for o in outcomes) / n,
        contact_rate=sum(o.contacted_customer for o in outcomes) / n,
        false_escalation_rate=false_escalation_rate,
        compliance_violations=violations,
        hard_decline_retry_waste=hard_waste,
        per_failure_code=per_code,
    )


def compute_for_run(
    result: RunResult, *, clv_paise: int = CUSTOMER_LIFETIME_VALUE_PAISE
) -> Metrics:
    """Metrics for a completed run, carrying its compliance violation count."""
    return compute(result.outcomes, violations=len(result.violations), clv_paise=clv_paise)


def annoyance_price_note(clv_paise: int = CUSTOMER_LIFETIME_VALUE_PAISE) -> str:
    """One line naming the most contestable assumption, for the report."""
    return (
        f"annoyance priced through churn against a lifetime value of INR "
        f"{clv_paise / 100:,.0f} (an ASSUMPTION, not a measurement), giving INR "
        f"{annoyance_paise_per_unit(clv_paise) / 100:,.0f} per annoyance unit; "
        "run --clv-sweep to see which conclusions survive changing it"
    )


# ---------------------------------------------------------------------------
# dataset ceilings
# ---------------------------------------------------------------------------
#
# These describe the DATA, not any policy, and that distinction is the whole
# reason they live here rather than being expressed as another entry in the
# policy registry. A policy that reaches for an action the invoice cannot pay
# for has its plan truncated by the compliance gate, so it measures neither
# ceiling - it just scores badly. The only way to state the cap-free ceiling
# honestly is to compute it directly, as the generator's own report does.


@dataclass(frozen=True, slots=True)
class Ceilings:
    """How much of this population was ever recoverable, two ways."""

    cap_free: float
    """Any action recovers, ignoring debit-attempt caps. Matches the generator's
    `summary.md` ceiling, and is not achievable by any compliant system."""

    cap_limited: float
    """Any action the invoice's remaining attempt budget can pay for. This is
    the real ceiling a policy can be held to."""

    @property
    def cost_of_caps_pp(self) -> float:
        """Recovery that scheme and mandate caps cost the merchant, in points."""
        return (self.cap_free - self.cap_limited) * 100.0

    def to_dict(self) -> dict[str, float]:
        return {
            "cap_free": round(self.cap_free, 6),
            "cap_limited": round(self.cap_limited, 6),
            "cost_of_caps_pp": round(self.cost_of_caps_pp, 4),
        }


def ceilings(transactions: Sequence[ObservedTransaction], store: Mapping[str, Any]) -> Ceilings:
    """Compute both recovery ceilings for a population of transactions."""
    n = len(transactions)
    if n == 0:
        return Ceilings(cap_free=0.0, cap_limited=0.0)

    free = limited = 0
    for txn in transactions:
        outcomes = store[txn.txn_id]["outcomes"]
        budget = txn.attempts_left
        recovering = [a for a in ACTIONS if outcomes[a]["recovered"]]
        free += bool(recovering)
        limited += any(ATTEMPTS_CONSUMED[a] <= budget for a in recovering)
    return Ceilings(cap_free=free / n, cap_limited=limited / n)
