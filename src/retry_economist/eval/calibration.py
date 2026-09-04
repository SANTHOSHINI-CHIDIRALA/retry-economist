"""Are the router's probability estimates any good?

This is the measurement that decides whether the router is worth having. Its
plan is replaceable - a lookup table produces plans, and a good one. What a
lookup table cannot produce is `p_recover_if_act` and `p_recover_if_abstain`,
and without those two numbers the economist layer in the next phase has nothing
to compute an expected value from. `rules_only` takes 421 unproductive actions
precisely because it has no probability to weigh.

So the estimates are scored like any other forecast: Brier score against ground
truth, a reliability table, and two reference predictors that must be beaten
for the estimates to be worth anything.

    (a) a constant base rate     - the "always predict the average" forecaster
    (b) a per-failure-code prior - what a PSP could build from its own logs

Reference (b) is fitted on the TRAIN split ONLY. Fitting it on the holdout would
let it see the very outcomes it is being scored against, making it artificially
strong and the comparison meaningless. A test asserts no holdout customer
contributes to the fit.

Two honest restrictions on scope:

- `p_recover_if_act` is only scored where the router actually proposed acting.
  Where it proposed nothing there is no "act" for the estimate to be about, and
  scoring those against the counterfactual would grade a forecast nobody made.
- `p_recover_if_abstain` is scored everywhere, because `would_pay_anyway` is
  defined for every transaction.

A null result here is a finding. If the estimates lose to a per-code prior, that
says the router's probabilities add nothing over a lookup, which is exactly what
the next phase needs to know before building on them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from retry_economist.policies.base import ObservedTransaction
from retry_economist.schema import ACTIONS

N_BINS = 10


# ---------------------------------------------------------------------------
# ground truth
# ---------------------------------------------------------------------------


def plan_recovers(plan: Sequence[str], outcomes: Mapping[str, Any]) -> bool:
    """Would this ordered plan have recovered, per the counterfactual store?

    Same walk the simulator performs: take the first action that succeeds.
    """
    return any(outcomes[action]["recovered"] for action in plan if action in outcomes)


@dataclass(frozen=True, slots=True)
class CalibrationRecord:
    """One forecast pair and the outcomes it is scored against."""

    txn_id: str
    customer_id: str
    failure_code: str
    proposed_plan: tuple[str, ...]
    p_act: float
    p_abstain: float
    #: Whether the proposed plan would have recovered. None when nothing was
    #: proposed, in which case this record does not enter the act-side scoring.
    act_outcome: bool | None
    abstain_outcome: bool

    @property
    def scores_act(self) -> bool:
        return self.act_outcome is not None


def build_records(
    proposals: Mapping[str, Any],
    transactions: Sequence[ObservedTransaction],
    store: Mapping[str, Any],
) -> list[CalibrationRecord]:
    """Pair each proposal with the ground truth the oracle holds."""
    by_id = {t.txn_id: t for t in transactions}
    records: list[CalibrationRecord] = []
    for txn_id, proposal in proposals.items():
        txn = by_id.get(txn_id)
        if txn is None:
            continue
        outcomes = store[txn_id]["outcomes"]
        plan = tuple(proposal.proposed_plan)
        records.append(
            CalibrationRecord(
                txn_id=txn_id,
                customer_id=txn.customer_id,
                failure_code=txn.failure_code,
                proposed_plan=plan,
                p_act=float(proposal.p_recover_if_act),
                p_abstain=float(proposal.p_recover_if_abstain),
                act_outcome=plan_recovers(plan, outcomes) if plan else None,
                abstain_outcome=bool(store[txn_id]["would_pay_anyway"]),
            )
        )
    return records


# ---------------------------------------------------------------------------
# reference predictors
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HistoricalPrior:
    """What a PSP could forecast from its own historical logs.

    Fitted on TRAIN ONLY, and it records which customers it saw so that the
    no-contamination rule is testable rather than merely intended.
    """

    abstain_by_code: dict[str, float]
    act_by_code_action: dict[tuple[str, str], float]
    act_by_code: dict[str, float]
    global_abstain: float
    global_act: float
    fitted_on_customers: frozenset[str]
    n_fitted: int

    @classmethod
    def fit(
        cls, train: Sequence[ObservedTransaction], store: Mapping[str, Any]
    ) -> "HistoricalPrior":
        abstain_hits: dict[str, list[int]] = {}
        act_hits: dict[tuple[str, str], list[int]] = {}
        code_hits: dict[str, list[int]] = {}
        all_abstain: list[int] = []
        all_act: list[int] = []

        for txn in train:
            record = store[txn.txn_id]
            outcomes = record["outcomes"]
            paid = int(bool(record["would_pay_anyway"]))
            abstain_hits.setdefault(txn.failure_code, []).append(paid)
            all_abstain.append(paid)

            for action in ACTIONS:
                if action == "do_nothing":
                    continue
                hit = int(bool(outcomes[action]["recovered"]))
                act_hits.setdefault((txn.failure_code, action), []).append(hit)
                code_hits.setdefault(txn.failure_code, []).append(hit)
                all_act.append(hit)

        mean = lambda xs: sum(xs) / len(xs) if xs else 0.0  # noqa: E731
        return cls(
            abstain_by_code={k: mean(v) for k, v in abstain_hits.items()},
            act_by_code_action={k: mean(v) for k, v in act_hits.items()},
            act_by_code={k: mean(v) for k, v in code_hits.items()},
            global_abstain=mean(all_abstain),
            global_act=mean(all_act),
            fitted_on_customers=frozenset(t.customer_id for t in train),
            n_fitted=len(train),
        )

    def predict_abstain(self, record: CalibrationRecord) -> float:
        return self.abstain_by_code.get(record.failure_code, self.global_abstain)

    def predict_act(self, record: CalibrationRecord) -> float:
        """Plan-aware: the historical rate for this code and this first action.

        A prior that ignored the action would be scored against outcomes it
        could not possibly track, which would make it a strawman rather than a
        baseline.
        """
        if record.proposed_plan:
            key = (record.failure_code, record.proposed_plan[0])
            if key in self.act_by_code_action:
                return self.act_by_code_action[key]
        return self.act_by_code.get(record.failure_code, self.global_act)


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


def brier(predictions: Sequence[float], outcomes: Sequence[bool]) -> float | None:
    """Mean squared error of a probabilistic forecast. Lower is better."""
    if not predictions:
        return None
    return sum((p - int(y)) ** 2 for p, y in zip(predictions, outcomes)) / len(predictions)


@dataclass(frozen=True, slots=True)
class ReliabilityBin:
    lower: float
    upper: float
    count: int
    mean_predicted: float | None
    observed_frequency: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bin": f"[{self.lower:.1f}, {self.upper:.1f})",
            "count": self.count,
            "mean_predicted": (
                None if self.mean_predicted is None else round(self.mean_predicted, 4)
            ),
            "observed_frequency": (
                None if self.observed_frequency is None else round(self.observed_frequency, 4)
            ),
            "gap": (
                None
                if self.mean_predicted is None or self.observed_frequency is None
                else round(self.mean_predicted - self.observed_frequency, 4)
            ),
        }


def reliability_table(
    predictions: Sequence[float], outcomes: Sequence[bool], bins: int = N_BINS
) -> list[ReliabilityBin]:
    """Mean predicted against observed frequency, in equal-width bins.

    The Brier score says how wrong a forecaster is; this says in which
    direction. A model that is right on average but confidently wrong at the
    extremes looks fine on Brier alone.
    """
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for p, y in zip(predictions, outcomes):
        idx = min(bins - 1, int(p * bins))
        buckets[idx].append((p, y))

    table: list[ReliabilityBin] = []
    for i, bucket in enumerate(buckets):
        lower, upper = i / bins, (i + 1) / bins
        if not bucket:
            table.append(ReliabilityBin(lower, upper, 0, None, None))
            continue
        table.append(
            ReliabilityBin(
                lower,
                upper,
                len(bucket),
                sum(p for p, _ in bucket) / len(bucket),
                sum(int(y) for _, y in bucket) / len(bucket),
            )
        )
    return table


@dataclass(frozen=True, slots=True)
class EstimateScore:
    """One estimate, scored against itself and both references."""

    label: str
    n: int
    router_brier: float | None
    constant_brier: float | None
    historical_brier: float | None
    base_rate: float
    reliability: list[ReliabilityBin] = field(default_factory=list)

    @property
    def beats_historical(self) -> bool | None:
        if self.router_brier is None or self.historical_brier is None:
            return None
        return self.router_brier < self.historical_brier

    @property
    def beats_constant(self) -> bool | None:
        if self.router_brier is None or self.constant_brier is None:
            return None
        return self.router_brier < self.constant_brier

    def verdict(self) -> str:
        """Said plainly, in whichever direction the numbers fall."""
        if self.router_brier is None:
            return f"{self.label}: no scored transactions"
        if self.beats_historical:
            return (
                f"{self.label}: router Brier {self.router_brier:.4f} BEATS the per-code "
                f"historical prior {self.historical_brier:.4f}"
            )
        return (
            f"{self.label}: router Brier {self.router_brier:.4f} does NOT beat the per-code "
            f"historical prior {self.historical_brier:.4f} - the estimates add nothing "
            "over a lookup on this data"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "n": self.n,
            "base_rate": round(self.base_rate, 6),
            "brier": {
                "router": None if self.router_brier is None else round(self.router_brier, 6),
                "constant_base_rate": (
                    None if self.constant_brier is None else round(self.constant_brier, 6)
                ),
                "historical_prior_train_only": (
                    None if self.historical_brier is None else round(self.historical_brier, 6)
                ),
            },
            "beats_constant": self.beats_constant,
            "beats_historical_prior": self.beats_historical,
            "reliability": [b.to_dict() for b in self.reliability],
        }


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    act: EstimateScore
    abstain: EstimateScore
    n_records: int
    n_act_scored: int
    n_abstain_scored: int
    prior_fitted_on: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_records": self.n_records,
            "n_act_scored": self.n_act_scored,
            "n_abstain_scored": self.n_abstain_scored,
            "prior_fitted_on_train_transactions": self.prior_fitted_on,
            "p_recover_if_act": self.act.to_dict(),
            "p_recover_if_abstain": self.abstain.to_dict(),
        }

    def verdicts(self) -> list[str]:
        return [self.act.verdict(), self.abstain.verdict()]


def evaluate(records: Sequence[CalibrationRecord], prior: HistoricalPrior) -> CalibrationReport:
    """Score both estimates against both references."""
    act_records = [r for r in records if r.scores_act]
    act_outcomes = [bool(r.act_outcome) for r in act_records]
    act_router = [r.p_act for r in act_records]
    act_constant = [prior.global_act] * len(act_records)
    act_historical = [prior.predict_act(r) for r in act_records]

    abstain_outcomes = [r.abstain_outcome for r in records]
    abstain_router = [r.p_abstain for r in records]
    abstain_constant = [prior.global_abstain] * len(records)
    abstain_historical = [prior.predict_abstain(r) for r in records]

    act = EstimateScore(
        label="p_recover_if_act",
        n=len(act_records),
        router_brier=brier(act_router, act_outcomes),
        constant_brier=brier(act_constant, act_outcomes),
        historical_brier=brier(act_historical, act_outcomes),
        base_rate=(sum(act_outcomes) / len(act_outcomes)) if act_outcomes else 0.0,
        reliability=reliability_table(act_router, act_outcomes),
    )
    abstain = EstimateScore(
        label="p_recover_if_abstain",
        n=len(records),
        router_brier=brier(abstain_router, abstain_outcomes),
        constant_brier=brier(abstain_constant, abstain_outcomes),
        historical_brier=brier(abstain_historical, abstain_outcomes),
        base_rate=(sum(abstain_outcomes) / len(abstain_outcomes)) if abstain_outcomes else 0.0,
        reliability=reliability_table(abstain_router, abstain_outcomes),
    )
    return CalibrationReport(
        act=act,
        abstain=abstain,
        n_records=len(records),
        n_act_scored=len(act_records),
        n_abstain_scored=len(records),
        prior_fitted_on=prior.n_fitted,
    )
