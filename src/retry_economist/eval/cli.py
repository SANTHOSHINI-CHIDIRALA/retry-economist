"""The scoreboard: run policies over a split and report what they actually bought.

    python -m retry_economist.eval.cli --split holdout --policies do_nothing,oracle_best

Three reporting rules are enforced here rather than left to the reader.

Cheating policies are quarantined. Anything flagged `is_reference_bound` is
rendered in its own "reference bounds" section, below the results, with its name
carrying "(CHEATS)". A bound tells you how much was ever available to win; it is
not a result, and it must not be possible to skim this report and mistake one
for the other.

The headline sentence is generated from the numbers, never written by hand, and
refuses to be generated at all unless it can compare one honest policy against
another. It may never take a cheating bound as its comparison, and it may never
report a subject that scores below the thing it is compared to.

The most contestable assumption is priced, not hidden. Customer lifetime value
is a guess, the annoyance cost scales linearly with it, and `--clv-sweep`
re-prices every policy across a wide range so the report can state which
conclusions survive the guess and which are artefacts of it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from retry_economist.eval import bootstrap as bs
from retry_economist.eval import calibration as cal
from retry_economist.eval import metrics as mx
from retry_economist.eval.costs import CUSTOMER_LIFETIME_VALUE_PAISE, cost_constants
from retry_economist.eval.simulator import (
    RunResult,
    filter_split,
    load_counterfactuals,
    load_observed,
    run,
)
from retry_economist.economist.estimator import HistoricalPriorEstimator
from retry_economist.economist.timing import DAILY_DISCOUNT_RATE
from retry_economist.generator.cli import generate
from retry_economist.policies.base import ObservedTransaction, Policy
from retry_economist.llm.cache import DEFAULT_CACHE_DIR, ResponseCache
from retry_economist.llm.provider import CachingProvider, MockProvider, ProviderUnavailable
from retry_economist.policies.do_nothing import DoNothingPolicy
from retry_economist.policies.llm_router_only import LLMRouterOnlyPolicy
from retry_economist.policies.naive_retry import NaiveRetry3xPolicy
from retry_economist.policies.oracle_best import OracleBestPolicy
from retry_economist.policies.retry_economist_naive_plan import RetryEconomistNaivePlanPolicy
from retry_economist.policies.retry_economist_prior import RetryEconomistPriorPolicy
from retry_economist.policies.rules_only import RulesOnlyPolicy
from retry_economist.router.router import Router
from retry_economist.router.signals import SignalIndex

DEFAULT_DATA_DIR = Path("data/generated")
DEFAULT_RESULTS_DIR = Path("results")
DEFAULT_SEED = 42
#: The canonical dataset scale. Larger than the generator's own defaults on
#: purpose: the bootstrap resamples customers, so the width of every interval is
#: set by how many customer clusters the holdout contains, not by how many
#: transactions it holds.
DEFAULT_N = 2500
DEFAULT_CUSTOMERS = 900

#: Lifetime values the sensitivity sweep re-prices at: INR 4,000 / 12,000 / 30,000.
CLV_SWEEP_PAISE: tuple[int, ...] = (400_000, 1_200_000, 3_000_000)

#: Daily discount rates the sensitivity sweep RE-DECIDES at (not merely
#: re-prices): 0.5% / 2% / 5%. Unlike CLV, this can change which plans clear
#: the EV bar, so - see `run_discount_rate_sweep` - each point re-runs the
#: policy rather than re-weighting a fixed set of outcomes.
DISCOUNT_RATE_SWEEP: tuple[float, ...] = (0.005, 0.02, 0.05)

RETRY_ECONOMIST_PRIOR_NAME = "retry_economist (prior)"
RETRY_ECONOMIST_NAIVE_PLAN_NAME = "retry_economist (naive plan)"

#: Every plan-source name that needs the train-only historical prior fitted
#: before `build_policies` runs - see `_PRIOR_CONTEXT`.
PRIOR_BASED_POLICIES: tuple[str, ...] = (RETRY_ECONOMIST_PRIOR_NAME, RETRY_ECONOMIST_NAIVE_PLAN_NAME)

#: Named constructors, so `--policies` takes short names and the counterfactual
#: store is injected explicitly into anything that needs it.
POLICY_BUILDERS: dict[str, Callable[[Mapping[str, Any]], Policy]] = {
    "do_nothing": lambda store: DoNothingPolicy(),
    "naive_retry_3x": lambda store: NaiveRetry3xPolicy(),
    "rules_only": lambda store: RulesOnlyPolicy(),
    "oracle_best": lambda store: OracleBestPolicy(_outcomes_by_txn(store)),
}

#: Policies needing the whole observed feed (not just the counterfactual store)
#: to build their signals. Constructed separately in `build_policies`.
FEED_POLICY_BUILDERS: dict[str, Any] = {
    # Bound lazily: the builder is defined below, and a direct reference here
    # would resolve at import time, before it exists.
    "llm_router_only": lambda transactions: _build_router_policy(transactions),
    RETRY_ECONOMIST_PRIOR_NAME: lambda transactions: _build_retry_economist_prior_policy(transactions),
    RETRY_ECONOMIST_NAIVE_PLAN_NAME: (
        lambda transactions: _build_retry_economist_naive_plan_policy(transactions)
    ),
}

#: The incumbent. Fixed-schedule retry is what production dunning systems
#: actually do, so it - not abstention - is the bar a new policy has to clear,
#: and the headline compares against it whenever it is in the run.
INCUMBENT_POLICY = "naive_retry_3x"

#: Comparisons the report always runs when both arms are present. Paired, so the
#: same customers are resampled for both sides of each difference.
PAIRED_COMPARISONS: tuple[tuple[str, str], ...] = (
    ("rules_only", "naive_retry_3x"),
    ("rules_only", "do_nothing"),
    ("llm_router_only (NO ECONOMIST)", "rules_only"),
    ("llm_router_only (NO ECONOMIST)", "naive_retry_3x"),
    (RETRY_ECONOMIST_PRIOR_NAME, "rules_only"),
    (RETRY_ECONOMIST_PRIOR_NAME, "naive_retry_3x"),
    (RETRY_ECONOMIST_NAIVE_PLAN_NAME, "naive_retry_3x"),
)


def build_provider(prefer_real: bool = True) -> tuple[Any, str]:
    """The provider to route with, and a label naming it.

    Falls back to the deterministic stand-in when no key or no pinned model is
    available. The label is threaded into every report: a number produced by the
    stand-in must never be presented as a language model result.
    """
    if prefer_real:
        try:
            from retry_economist.llm.provider import GeminiProvider

            inner: Any = GeminiProvider()
            return CachingProvider(inner, ResponseCache(DEFAULT_CACHE_DIR)), f"gemini:{inner.model}"
        except (ProviderUnavailable, ImportError):
            pass
    mock = MockProvider()
    return CachingProvider(mock, ResponseCache(DEFAULT_CACHE_DIR)), "mock-deterministic (NOT an LLM)"


#: Populated when a routing policy is built, so the report can name the provider
#: and quote its counters without the policy having to carry them.
_ROUTER_CONTEXT: dict[str, Any] = {}


def _build_router_policy(
    transactions: Sequence[ObservedTransaction], *, prefer_real: bool = True
) -> LLMRouterOnlyPolicy:
    provider, label = build_provider(prefer_real)
    router = Router(provider, SignalIndex(transactions))
    policy = LLMRouterOnlyPolicy(router)
    _ROUTER_CONTEXT.update({"provider": provider, "label": label, "router": router, "policy": policy})
    return policy


#: Populated by `main()` before `evaluate()` runs, so
#: `_build_retry_economist_prior_policy` (a `FEED_POLICY_BUILDERS` closure that
#: only receives `transactions`) can reach the fitted prior and the discount
#: rate for this run - same pattern as `_ROUTER_CONTEXT` above.
_PRIOR_CONTEXT: dict[str, Any] = {}


def historical_prior_estimator(prior: cal.HistoricalPrior) -> HistoricalPriorEstimator:
    """Unpack a fitted `HistoricalPrior` into the plain-data estimator the
    economist package accepts - see `economist/estimator.py`'s docstring for
    why the unpacking happens here rather than inside that package."""
    return HistoricalPriorEstimator(
        abstain_by_code=dict(prior.abstain_by_code),
        act_by_code_action=dict(prior.act_by_code_action),
        act_by_code=dict(prior.act_by_code),
        global_abstain=prior.global_abstain,
        global_act=prior.global_act,
    )


def _build_retry_economist_prior_policy(
    transactions: Sequence[ObservedTransaction],
) -> RetryEconomistPriorPolicy:
    estimator = _PRIOR_CONTEXT.get("estimator")
    if estimator is None:
        raise SystemExit(
            f"{RETRY_ECONOMIST_PRIOR_NAME!r} needs a historical prior fitted on train data "
            "before it can be built; this is an internal ordering bug in main()"
        )
    rate = _PRIOR_CONTEXT.get("daily_discount_rate", DAILY_DISCOUNT_RATE)
    policy = RetryEconomistPriorPolicy(
        SignalIndex(transactions), estimator, daily_discount_rate=rate
    )
    # Keyed by name, not overwritten by the naive-plan builder below - both
    # can be built in the same run (the six-policy scoreboard does exactly
    # that), and each is kept for the report to audit without re-deciding.
    _PRIOR_CONTEXT.setdefault("policies", {})[RETRY_ECONOMIST_PRIOR_NAME] = policy
    return policy


def _build_retry_economist_naive_plan_policy(
    transactions: Sequence[ObservedTransaction],
) -> RetryEconomistNaivePlanPolicy:
    estimator = _PRIOR_CONTEXT.get("estimator")
    if estimator is None:
        raise SystemExit(
            f"{RETRY_ECONOMIST_NAIVE_PLAN_NAME!r} needs a historical prior fitted on train data "
            "before it can be built; this is an internal ordering bug in main()"
        )
    rate = _PRIOR_CONTEXT.get("daily_discount_rate", DAILY_DISCOUNT_RATE)
    policy = RetryEconomistNaivePlanPolicy(
        SignalIndex(transactions), estimator, daily_discount_rate=rate
    )
    _PRIOR_CONTEXT.setdefault("policies", {})[RETRY_ECONOMIST_NAIVE_PLAN_NAME] = policy
    return policy


def _outcomes_by_txn(store: Mapping[str, Any]) -> dict[str, Any]:
    return {txn_id: row["outcomes"] for txn_id, row in store.items()}


def generator_hash() -> str:
    """Fingerprint of the code that produced the data.

    A scoreboard is only reproducible if the reader can tell which simulator
    wrote its inputs; the seed alone is not enough once the generator changes.
    """
    root = Path(__file__).resolve().parents[1]
    files = sorted((root / "generator").glob("*.py")) + [root / "schema.py"]
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class PolicyReport:
    """One policy's line on the scoreboard."""

    name: str
    is_reference_bound: bool
    metrics: mx.Metrics
    ci: bs.BootstrapResult
    result: RunResult

    @property
    def valid(self) -> bool:
        return self.result.is_valid

    @property
    def status(self) -> str:
        return "ok" if self.valid else "INVALID"


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------


def _pct(x: float, places: int = 1) -> str:
    return f"{x * 100:.{places}f}%"


def _money(rupees: float) -> str:
    return f"{rupees:,.0f}"


def _ratio(value: float | None, *, compact: bool = False) -> str:
    """Rupees spent per rupee of net new revenue.

    Kept to four places below 0.01 - an efficient policy's ratio is genuinely
    small, and rounding it to "0.00" throws away the very number the merchant
    is looking for.
    """
    if value is None:
        return "n/a" if compact else "n/a (no net revenue)"
    return f"{value:.4f}" if value < 0.01 else f"{value:.2f}"


def _rate(value: float | None) -> str:
    return "n/a" if value is None else _pct(value)


def _days(value: float | None, m: mx.Metrics) -> str:
    """Render a timing figure, keeping three different absences distinguishable.

    An empty slice, a slice that recovered nothing, and a real duration used to
    collapse into the same "n/a". They mean different things - no data, versus a
    policy that never succeeded - so they render differently and the JSON
    carries `n` and `n_recovered` alongside, so the distinction survives
    serialisation too.
    """
    if m.n == 0:
        return "-"
    if value is None:
        return "n/a (0 recovered)"
    return f"{value:.2f}"


def _ci_pct(ci: bs.ConfidenceInterval) -> str:
    if ci.point is None:
        return "n/a"
    return f"{ci.point * 100:.1f}% [{(ci.low or 0) * 100:.1f}, {(ci.high or 0) * 100:.1f}]"


def _ci_pp(ci: bs.ConfidenceInterval) -> str:
    if ci.point is None:
        return "n/a"
    return f"{ci.point:+.1f} [{ci.low:+.1f}, {ci.high:+.1f}]"


# ---------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------


def build_policies(
    names: Sequence[str],
    store: Mapping[str, Any],
    transactions: Sequence[ObservedTransaction] | None = None,
) -> list[Policy]:
    policies: list[Policy] = []
    known = set(POLICY_BUILDERS) | set(FEED_POLICY_BUILDERS)
    for name in names:
        if name in FEED_POLICY_BUILDERS:
            if transactions is None:
                raise SystemExit(f"policy {name!r} needs the observed feed to build its signals")
            policies.append(FEED_POLICY_BUILDERS[name](transactions))
        elif name in POLICY_BUILDERS:
            policies.append(POLICY_BUILDERS[name](store))
        else:
            raise SystemExit(f"unknown policy {name!r}; available: {', '.join(sorted(known))}")
    return policies


def evaluate(
    policy_names: Sequence[str],
    transactions: Sequence[ObservedTransaction],
    store: Mapping[str, Any],
    split: str,
    *,
    iterations: int,
    bootstrap_seed: int,
    clv_paise: int = CUSTOMER_LIFETIME_VALUE_PAISE,
) -> list[PolicyReport]:
    reports: list[PolicyReport] = []
    for policy in build_policies(policy_names, store, transactions):
        result = run(policy, transactions, store, split=split)
        reports.append(
            PolicyReport(
                name=policy.name,
                is_reference_bound=result.is_reference_bound,
                metrics=mx.compute_for_run(result, clv_paise=clv_paise),
                ci=bs.bootstrap_run(
                    result, iterations=iterations, seed=bootstrap_seed, clv_paise=clv_paise
                ),
                result=result,
            )
        )
    return reports


def ensure_data(
    data_dir: Path, *, seed: int, n: int, n_customers: int
) -> tuple[list[ObservedTransaction], dict[str, Any], dict[str, Any]]:
    """Load the dataset, generating it first if it is not already on disk."""
    observed_path = data_dir / "observed.jsonl"
    if not observed_path.exists():
        generate(seed=seed, n=n, n_customers=n_customers, out_dir=data_dir)
    return (
        load_observed(observed_path),
        load_counterfactuals(data_dir / "oracle.jsonl"),
        json.loads((data_dir / "splits.json").read_text(encoding="utf-8")),
    )


# ---------------------------------------------------------------------------
# headline
# ---------------------------------------------------------------------------

NO_HEADLINE = "not available - no baseline policy in this run"


def build_headline(reports: Sequence[PolicyReport]) -> str:
    """Generate the headline from the numbers, or refuse to generate one.

    A headline is a claim, so it is held to the standard of a claim:

    - the subject is the best-scoring policy that does not cheat;
    - the comparison is another honest policy, never a reference bound - saying
      a policy beat something that read the answers is not a result;
    - both must be compliant, since an INVALID policy's numbers are not usable;
    - and the subject may never score below its comparison, which would turn the
      sentence into a boast about losing.

    When those cannot be satisfied, this returns `NO_HEADLINE` and the report
    prints that and nothing else. An absent headline is honest; a headline
    comparing a policy to a cheat is not.
    """
    eligible = [r for r in reports if not r.is_reference_bound and r.valid]
    if len(eligible) < 2:
        return NO_HEADLINE

    subject = max(eligible, key=lambda r: (r.metrics.recovery_rate, -r.metrics.attempts_per_txn))
    others = [r for r in eligible if r.name != subject.name]
    if not others:
        return NO_HEADLINE

    # Prefer the incumbent: beating the policy a merchant is running today is
    # the claim that matters. Falling back to the least-interventionist honest
    # policy keeps the headline meaningful in runs that omit the incumbent.
    incumbent = next((r for r in others if r.name == INCUMBENT_POLICY), None)
    comparison = incumbent or min(others, key=lambda r: r.metrics.attempts_per_txn)

    if subject.metrics.recovery_rate < comparison.metrics.recovery_rate:
        return NO_HEADLINE  # unreachable by construction; kept as a hard stop

    head_attempts = subject.metrics.total_attempts
    comp_attempts = comparison.metrics.total_attempts

    if comp_attempts > 0:
        delta = abs(comp_attempts - head_attempts) / comp_attempts * 100.0
        if head_attempts < comp_attempts:
            clause = f"using {delta:.0f}% fewer retry attempts"
        elif head_attempts > comp_attempts:
            clause = f"using {delta:.0f}% more retry attempts"
        else:
            clause = "using the same number of retry attempts"
    elif head_attempts == 0:
        clause = "with neither policy spending a retry attempt"
    else:
        clause = f"using {head_attempts} retry attempts against a baseline that used none"

    return (
        f"Recovered {_pct(subject.metrics.recovery_rate)} vs "
        f"{_pct(comparison.metrics.recovery_rate)} for {comparison.name}, {clause}."
    )


# ---------------------------------------------------------------------------
# paired comparisons
# ---------------------------------------------------------------------------


def run_paired_comparisons(
    reports: Sequence[PolicyReport],
    pairs: Sequence[tuple[str, str]] = PAIRED_COMPARISONS,
    *,
    iterations: int = bs.DEFAULT_ITERATIONS,
    seed: int = bs.DEFAULT_SEED,
) -> list[dict[str, Any]]:
    """Paired CIs for the difference between two policies, same customers both sides.

    Paired rather than eyeballing two marginal intervals: both policies are
    scored on the same payers, so a draw containing several hard-blocked
    customers drags BOTH down, and that shared swing is not evidence about which
    policy is better. Differencing inside each iteration cancels it, which is
    why these intervals are much tighter than the overlap of the marginals - and
    why overlapping marginals routinely hide a real difference.
    """
    by_name = {r.name: r for r in reports}
    rows: list[dict[str, Any]] = []
    for subject_name, baseline_name in pairs:
        subject = by_name.get(subject_name)
        baseline = by_name.get(baseline_name)
        if subject is None or baseline is None:
            continue  # that pairing was not part of this run
        uplift = bs.paired_bootstrap(
            subject.result,
            baseline.result,
            iterations=iterations,
            seed=seed,
            statistic=bs.net_uplift_statistic,
        )
        f1 = bs.paired_bootstrap(
            subject.result,
            baseline.result,
            iterations=iterations,
            seed=seed,
            statistic=bs.decision_f1_statistic,
        )
        rows.append(
            {
                "subject": subject_name,
                "baseline": baseline_name,
                "net_uplift_pp_delta": uplift.to_dict(),
                "decision_f1_delta": f1.to_dict(),
                # An interval that excludes zero is a difference the data
                # supports; one that straddles it is not, and the report says so
                # rather than leaving the reader to compare two numbers.
                "uplift_significant": bool(
                    uplift.low is not None and (uplift.low > 0 or uplift.high < 0)
                ),
                "f1_significant": bool(f1.low is not None and (f1.low > 0 or f1.high < 0)),
            }
        )
    return rows


def render_paired(rows: Sequence[dict[str, Any]]) -> list[str]:
    lines = [
        "## Paired comparisons",
        "",
        "Difference between two policies, bootstrapped over the SAME resampled "
        "customers on both sides. An interval excluding zero is a difference the "
        "evidence supports.",
        "",
        "| comparison | net uplift pp delta (95% CI) | supported | decision F1 delta (95% CI) | "
        "supported |",
        "| --- | ---: | :---: | ---: | :---: |",
    ]
    for row in rows:
        u, f = row["net_uplift_pp_delta"], row["decision_f1_delta"]
        lines.append(
            f"| `{row['subject']}` vs `{row['baseline']}` | "
            f"{u['point']:+.2f} [{u['low']:+.2f}, {u['high']:+.2f}] | "
            f"{'yes' if row['uplift_significant'] else 'no'} | "
            f"{f['point']:+.4f} [{f['low']:+.4f}, {f['high']:+.4f}] | "
            f"{'yes' if row['f1_significant'] else 'no'} |"
        )
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# sensitivity sweep
# ---------------------------------------------------------------------------


def run_clv_sweep(
    reports: Sequence[PolicyReport], clvs: Sequence[int] = CLV_SWEEP_PAISE
) -> list[dict[str, Any]]:
    """Re-price every finished run across a range of lifetime values.

    No re-simulation: the outcomes are fixed, only what annoyance costs changes.
    Net REVENUE is invariant under this sweep by construction - it is recovered
    minus destroyed invoice value, and no assumption about churn touches it. Net
    VALUE, which subtracts what the recovery cost, is what moves, and it is the
    number a merchant would actually act on.
    """
    rows: list[dict[str, Any]] = []
    for report in reports:
        points = []
        for clv in clvs:
            m = mx.compute_for_run(report.result, clv_paise=clv)
            points.append(
                {
                    "clv_rupees": clv / 100,
                    "net_rupees": round(m.net_rupees, 2),
                    "annoyance_cost_rupees": round(m.annoyance_cost_rupees, 2),
                    "net_value_rupees": round(m.net_value_rupees, 2),
                    "cost_per_incremental_rupee": (
                        None
                        if m.cost_per_incremental_rupee is None
                        else round(m.cost_per_incremental_rupee, 4)
                    ),
                }
            )
        signs = {p["net_value_rupees"] > 0 for p in points}
        rows.append(
            {
                "policy": report.name,
                "is_reference_bound": report.is_reference_bound,
                "points": points,
                # A verdict, stated rather than left for the reader to infer.
                "verdict": (
                    "robust - net value keeps its sign across the whole range"
                    if len(signs) == 1
                    else "FRAGILE - net value changes sign within the range; this "
                    "conclusion is an artefact of the lifetime-value guess"
                ),
                "robust": len(signs) == 1,
            }
        )
    return rows


def sweep_conclusion(rows: Sequence[dict[str, Any]]) -> str:
    """Whether the ranking itself survives the sweep, not just each policy."""
    honest = [r for r in rows if not r["is_reference_bound"]]
    if len(honest) < 2:
        fragile = [r["policy"] for r in rows if not r["robust"]]
        if fragile:
            return (
                "At least one policy changes sign across the sweep: "
                + ", ".join(f"`{p}`" for p in fragile)
                + ". Those conclusions depend on the lifetime-value guess."
            )
        return "Every policy keeps the sign of its net value across the whole sweep."

    winners = set()
    for idx in range(len(honest[0]["points"])):
        winners.add(max(honest, key=lambda r: r["points"][idx]["net_value_rupees"])["policy"])
    if len(winners) == 1:
        return (
            f"The ranking survives the sweep: `{winners.pop()}` has the highest net value at "
            "every lifetime value tested, so the conclusion is about the policy, not the guess."
        )
    return (
        "The ranking FLIPS within the sweep (best policy varies: "
        + ", ".join(f"`{w}`" for w in sorted(winners))
        + "). The conclusion is an artefact of the lifetime-value assumption and must not "
        "be stated without it."
    )


def run_discount_rate_sweep(
    transactions: Sequence[ObservedTransaction],
    store: Mapping[str, Any],
    prior: cal.HistoricalPrior,
    split: str,
    *,
    rates: Sequence[float] = DISCOUNT_RATE_SWEEP,
    clv_paise: int = CUSTOMER_LIFETIME_VALUE_PAISE,
    reports: Sequence[PolicyReport] = (),
) -> list[dict[str, Any]]:
    """Re-DECIDE `retry_economist (prior)` at several daily discount rates.

    Cannot be a re-pricing of fixed outcomes the way `run_clv_sweep` is: the
    discount rate sits inside the EV threshold the economist checks BEFORE
    anything executes (see `economist/economist.py::compute_ev`), so a
    different rate can approve, truncate or veto a different set of
    transactions outright. Each point below builds a fresh policy at that
    rate and re-runs the simulator from scratch.
    """
    estimator = historical_prior_estimator(prior)
    baseline_uplift = {r.name: r.metrics.net_uplift_pp for r in reports if not r.is_reference_bound}

    points: list[dict[str, Any]] = []
    for rate in rates:
        policy = RetryEconomistPriorPolicy(
            SignalIndex(transactions), estimator, daily_discount_rate=rate
        )
        result = run(policy, transactions, store, split=split)
        m = mx.compute_for_run(result, clv_paise=clv_paise)
        points.append(
            {
                "daily_discount_rate": rate,
                "recovery_rate": round(m.recovery_rate, 4),
                "net_uplift_pp": round(m.net_uplift_pp, 3),
                "net_value_rupees": round(m.net_value_rupees, 2),
                "action_rate": round(m.action_rate, 4),
            }
        )

    advantage_over: dict[str, dict[str, Any]] = {}
    for baseline_name in ("rules_only", "naive_retry_3x"):
        base = baseline_uplift.get(baseline_name)
        if base is None:
            continue
        advantage_over[baseline_name] = {
            "baseline_net_uplift_pp": round(base, 3),
            "advantage_survives_all_rates": all(p["net_uplift_pp"] > base for p in points),
        }

    return [
        {
            "policy": RETRY_ECONOMIST_PRIOR_NAME,
            "points": points,
            "advantage_over": advantage_over,
        }
    ]


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

_RESULTS_HEADER = (
    "| policy | status | decision precision | decision recall | decision F1 | "
    "addressable capture | action selection error | "
    "precision (INR-wt) | recall (INR-wt) | F1 (INR-wt) | "
    "recovery rate (95% CI) | net uplift pp (95% CI) | "
    "recovery (INR-wt) | uplift pp (INR-wt) | median days | mean days | recovered <=72h | "
    "action rate | abstained | restraint precision | net INR | cost INR | net value INR | "
    "INR spent per INR earned | attempts | contact | viol |"
)
_RESULTS_RULE = "| --- | --- |" + " ---: |" * 23

_ATTRIB_HEADER = (
    "| policy | acted | incremental | cannibalised | wasted | futile (hopeless) | "
    "futile (wrong action) | abstained | correct restraint | correct walkaway | "
    "missed opportunity | sum |"
)
_ATTRIB_RULE = "| --- |" + " ---: |" * 11


def _results_row(report: PolicyReport) -> str:
    m = report.metrics
    d, rd = m.decision, m.rupee_decision
    return (
        f"| `{report.name}` | {report.status} | {_rate(d.precision)} | {_rate(d.recall)} | "
        f"{_rate(d.f1)} | {_rate(m.addressable_capture_rate)} | "
        f"{_rate(m.action_selection_error_rate)} | "
        f"{_rate(rd.precision)} | {_rate(rd.recall)} | {_rate(rd.f1)} | "
        f"{_ci_pct(report.ci.recovery_rate)} | "
        f"{_ci_pp(report.ci.net_uplift_pp)} | {_pct(m.rupee_recovery_rate)} | "
        f"{m.rupee_net_uplift_pp:+.1f} | {_days(m.median_days_to_recovery, m)} | "
        f"{_days(m.mean_days_to_recovery, m)} | {_pct(m.recovered_within_72h_rate)} | "
        f"{_pct(m.action_rate)} | {m.n_abstained} | "
        f"{_rate(m.restraint_precision)} | {_money(m.net_rupees)} | "
        f"{_money(m.total_cost_rupees + m.annoyance_cost_rupees)} | "
        f"{_money(m.net_value_rupees)} | {_ratio(m.cost_per_incremental_rupee)} | "
        f"{m.total_attempts} | {_pct(m.contact_rate)} | {m.compliance_violations} |"
    )


def _attribution_row(report: PolicyReport) -> str:
    m = report.metrics
    total = (
        m.incremental.count
        + m.cannibalised.count
        + m.wasted.count
        + m.futile.count
        + m.correct_restraint.count
        + m.correct_walkaway.count
        + m.missed_opportunity.count
    )
    return (
        f"| `{report.name}` | {m.n_acted} | {m.incremental.count} | {m.cannibalised.count} | "
        f"{m.wasted.count} | {m.futile_hopeless.count} | {m.wrong_action.count} | "
        f"{m.n_abstained} | {m.correct_restraint.count} | "
        f"{m.correct_walkaway.count} | {m.missed_opportunity.count} | {total}/{m.n} |"
    )


_RUPEE_ATTRIB_HEADER = (
    "| policy | at risk INR | incremental | cannibalised | wasted | futile | "
    "correct restraint | correct walkaway | missed opportunity | sum |"
)
_RUPEE_ATTRIB_RULE = "| --- |" + " ---: |" * 9


def _rupee_attribution_row(report: PolicyReport) -> str:
    """The same seven buckets, weighted by money rather than by invoice count."""
    m = report.metrics
    buckets = (
        m.incremental,
        m.cannibalised,
        m.wasted,
        m.futile,
        m.correct_restraint,
        m.correct_walkaway,
        m.missed_opportunity,
    )
    cells = " ".join(f"{_pct(b.rupee_share)} |" for b in buckets)
    return (
        f"| `{report.name}` | {_money(m.total_rupees_at_risk)} | {cells} "
        f"{_pct(sum(b.rupee_share for b in buckets))} |"
    )


def _per_mode_table(report: PolicyReport) -> list[str]:
    lines = [
        f"#### `{report.name}` by failure code",
        "",
        "| failure code | n | precision | recall | F1 | addressable | capture | "
        "selection error | precision (INR-wt) | recall (INR-wt) | "
        "F1 (INR-wt) | recovery | organic | uplift pp | recovery (INR-wt) | "
        "uplift pp (INR-wt) | median days | mean days | recovered <=72h | incr | cannib | "
        "hopeless | wrong action | missed | restraint precision | net INR | attempts |",
        "| --- | ---: |" + " ---: |" * 25,
    ]
    for code, m in sorted(
        report.metrics.per_failure_code.items(), key=lambda kv: (-kv[1].n, kv[0])
    ):
        lines.append(
            f"| `{code}` | {m.n} | {_rate(m.decision.precision)} | {_rate(m.decision.recall)} | "
            f"{_rate(m.decision.f1)} | {m.total_addressable} | "
            f"{_rate(m.addressable_capture_rate)} | {_rate(m.action_selection_error_rate)} | "
            f"{_rate(m.rupee_decision.precision)} | "
            f"{_rate(m.rupee_decision.recall)} | {_rate(m.rupee_decision.f1)} | "
            f"{_pct(m.recovery_rate)} | {_pct(m.organic_rate)} | "
            f"{m.net_uplift_pp:+.1f} | {_pct(m.rupee_recovery_rate)} | "
            f"{m.rupee_net_uplift_pp:+.1f} | {_days(m.median_days_to_recovery, m)} | "
            f"{_days(m.mean_days_to_recovery, m)} | {_pct(m.recovered_within_72h_rate)} | "
            f"{m.incremental.count} | {m.cannibalised.count} | "
            f"{m.futile_hopeless.count} | {m.wrong_action.count} | "
            f"{m.missed_opportunity.count} | {_rate(m.restraint_precision)} | "
            f"{_money(m.net_rupees)} | {m.total_attempts} |"
        )
    lines.append("")
    return lines


def render_markdown(
    reports: Sequence[PolicyReport],
    *,
    split: str,
    n_txns: int,
    n_customers: int,
    seed: int,
    iterations: int,
    bootstrap_seed: int,
    ceilings: mx.Ceilings,
    clv_paise: int = CUSTOMER_LIFETIME_VALUE_PAISE,
    paired: Sequence[dict[str, Any]] | None = None,
    multiseed: Sequence[dict[str, Any]] | None = None,
    clv_sweep: Sequence[dict[str, Any]] | None = None,
    discount_sweep: Sequence[dict[str, Any]] | None = None,
    calibration: Any = None,
    provider: str | None = None,
    router_stats: dict[str, Any] | None = None,
    provider_stats: dict[str, Any] | None = None,
) -> str:
    honest = [r for r in reports if not r.is_reference_bound]
    bounds = [r for r in reports if r.is_reference_bound]
    invalid = [r for r in reports if not r.valid]
    headline = build_headline(reports)

    lines = [
        f"# Retry Economist - scoreboard ({split})",
        "",
        f"- split: **{split}**",
        f"- transactions: **{n_txns}**",
        f"- customers: **{n_customers}**",
        f"- data seed: `{seed}`",
        f"- generator hash: `{generator_hash()}`",
        f"- bootstrap: {iterations} clustered iterations (resampling customers), "
        f"seed `{bootstrap_seed}`",
        *( [
            f"- LLM provider: **{provider}**",
        ] if provider else []),
        f"- recoverable ceiling: **{_pct(ceilings.cap_limited)}** within debit-attempt caps, "
        f"{_pct(ceilings.cap_free)} ignoring them "
        f"(scheme caps cost {ceilings.cost_of_caps_pp:.1f} pp of recovery outright)",
        "",
        "## HEADLINE",
        "",
        f"> HEADLINE: {headline}" if headline == NO_HEADLINE else f"> {headline}",
        "",
    ]

    if invalid:
        lines += [
            "> **INVALID POLICIES PRESENT.** "
            + ", ".join(
                f"`{r.name}` ({r.metrics.compliance_violations} violations)" for r in invalid
            )
            + " asked to exceed an invoice's debit-attempt cap. Their numbers are "
            "reported but must not be used: the plans were truncated to stay legal.",
            "",
        ]

    lines += ["## Results", ""]
    if honest:
        lines += [_RESULTS_HEADER, _RESULTS_RULE] + [_results_row(r) for r in honest] + [""]
    else:
        lines += ["_No honest policies in this run._", ""]

    if bounds:
        lines += [
            "## Reference bounds - NOT RESULTS",
            "",
            "These read the counterfactual outcomes to pick an action already known to work.",
            "No deployable policy can do this; they are here to show how much was ever",
            "available to win.",
            "",
            _RESULTS_HEADER,
            _RESULTS_RULE,
        ] + [_results_row(r) for r in bounds] + [""]

    lines += [
        "## Attribution",
        "",
        "Split first on whether the policy acted at all. Restraint is not failure: "
        "leaving a customer alone who pays unaided, or one no available action could "
        "have recovered, is the system working - at zero cost. "
        "**Restraint precision** is the share of untouched transactions that fall in "
        "those two buckets."
        "\n\n"
        "`futile` is split by whose mistake it was. **hopeless** means no affordable "
        "action would ever have recovered it, so the spend should not have been "
        "authorised at all - an economics failure. **wrong action** means the "
        "opportunity was real and the wrong action was chosen - a routing failure. "
        "`action selection error` is the second as a share of the transactions the "
        "policy was right to act on.",
        "",
        _ATTRIB_HEADER,
        _ATTRIB_RULE,
    ] + [_attribution_row(r) for r in reports] + [
        "",
        "### Weighted by rupees at risk",
        "",
        "The same seven buckets as a share of the money, not of the invoice count. "
        "Amounts here span a median around INR 700 to a p95 above INR 31,000, so the "
        "two views can rank policies differently - and where they diverge, the "
        "divergence is the finding, not a rounding artefact.",
        "",
        _RUPEE_ATTRIB_HEADER,
        _RUPEE_ATTRIB_RULE,
    ] + [_rupee_attribution_row(r) for r in reports] + [""]

    if paired:
        lines += render_paired(paired)

    lines += ["## Breakdown by failure code", ""]
    for report in reports:
        lines += _per_mode_table(report)

    if multiseed:
        lines += render_multiseed(multiseed)

    if clv_sweep:
        lines += render_clv_sweep(clv_sweep)

    if discount_sweep:
        lines += render_discount_sweep(discount_sweep)

    if router_stats or provider_stats:
        lines += render_router_stats(router_stats, provider_stats, provider)

    if calibration is not None:
        lines += render_calibration(calibration)

    lines += [
        "## Cost assumptions",
        "",
        "Every figure below is an estimate; see `eval/costs.py` for the basis of each.",
        "",
        "| constant | value |",
        "| --- | ---: |",
    ]
    for key, value in cost_constants().items():
        lines.append(f"| `{key}` | {value} |")
    lines += ["", f"_{mx.annoyance_price_note(clv_paise)}._", ""]

    return "\n".join(lines)


def render_multiseed(rows: Sequence[dict[str, Any]]) -> list[str]:
    """Mean and full range per policy across regenerated datasets."""
    lines = [
        "## Robustness across seeds",
        "",
        "Each seed regenerates the entire world - issuers, customers, failures and "
        "counterfactuals - and reruns every policy. Mean with full observed range.",
        "",
        "| policy | seeds | recovery rate | net uplift pp | restraint precision | "
        "net INR | attempts/txn | violations |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| `{row['policy']}` | {row['n_seeds']} | "
            f"{_pct(row['recovery_rate_mean'])} "
            f"[{_pct(row['recovery_rate_min'])}, {_pct(row['recovery_rate_max'])}] | "
            f"{row['net_uplift_pp_mean']:+.1f} "
            f"[{row['net_uplift_pp_min']:+.1f}, {row['net_uplift_pp_max']:+.1f}] | "
            f"{_rate(row['restraint_precision_mean'])} | "
            f"{_money(row['net_rupees_mean'])} "
            f"[{_money(row['net_rupees_min'])}, {_money(row['net_rupees_max'])}] | "
            f"{row['attempts_per_txn_mean']:.2f} | {row['violations_total']} |"
        )
    lines.append("")
    return lines


def render_clv_sweep(rows: Sequence[dict[str, Any]]) -> list[str]:
    lines = [
        "## Sensitivity to customer lifetime value",
        "",
        "Customer lifetime value is an ASSUMPTION, not a measurement, and annoyance "
        "cost scales linearly with it. Each policy below is re-priced across a range "
        "wide enough to cover any plausible value. Net *revenue* is invariant by "
        "construction - no churn assumption touches it - so net *value*, which "
        "subtracts what the recovery cost, is the column that moves.",
        "",
        f"> {sweep_conclusion(rows)}",
        "",
        "| policy | CLV (INR) | net revenue INR | annoyance cost INR | net value INR | "
        "INR spent per INR earned |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        label = f"{row['policy']}{' (bound)' if row['is_reference_bound'] else ''}"
        for point in row["points"]:
            lines.append(
                f"| `{label}` | {point['clv_rupees']:,.0f} | {_money(point['net_rupees'])} | "
                f"{_money(point['annoyance_cost_rupees'])} | "
                f"{_money(point['net_value_rupees'])} | "
                f"{_ratio(point['cost_per_incremental_rupee'])} |"
            )
    lines.append("")
    lines.append("| policy | verdict |")
    lines.append("| --- | --- |")
    for row in rows:
        lines.append(f"| `{row['policy']}` | {row['verdict']} |")
    lines.append("")
    return lines


def render_discount_sweep(rows: Sequence[dict[str, Any]]) -> list[str]:
    lines = [
        "## Sensitivity to the daily discount rate",
        "",
        f"`{RETRY_ECONOMIST_PRIOR_NAME}` is RE-DECIDED at each rate, not re-priced like "
        "the CLV sweep above: the discount factor sits inside the EV threshold the "
        "economist checks before anything executes, so a different rate can change "
        "which transactions are approved, truncated or vetoed outright - the "
        "executed plans differ, not just how a fixed set of plans is valued.",
        "",
        "| daily rate | recovery | net uplift pp | net value INR | action rate |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        for point in row["points"]:
            lines.append(
                f"| {point['daily_discount_rate']:.3f} | {_pct(point['recovery_rate'])} | "
                f"{point['net_uplift_pp']:+.2f} | {_money(point['net_value_rupees'])} | "
                f"{_pct(point['action_rate'])} |"
            )
        lines.append("")
        for baseline, info in row["advantage_over"].items():
            verdict = (
                "SURVIVES every rate tested"
                if info["advantage_survives_all_rates"]
                else "DOES NOT SURVIVE every rate tested"
            )
            lines.append(
                f"> vs `{baseline}` (net uplift {info['baseline_net_uplift_pp']:+.2f} pp): "
                f"advantage {verdict}."
            )
        lines.append("")
    return lines


def render_router_stats(
    router_stats: dict[str, Any] | None,
    provider_stats: dict[str, Any] | None,
    provider: str | None,
) -> list[str]:
    lines = ["## Router and provider", ""]
    if provider and "mock" in provider:
        lines += [
            "> **These proposals did NOT come from a language model.** No API key was "
            "available, so the deterministic stand-in produced them. The architecture is "
            "identical - only the source of the proposals and probabilities differs - but "
            "nothing here is evidence about what an LLM would do.",
            "",
        ]
    lines += [f"- provider: `{provider}`"] if provider else []
    if router_stats:
        lines += [
            f"- proposals: {router_stats['proposals']}",
            f"- parse failures (degraded to abstain): **{router_stats['parse_failures']}**",
            f"- schema violations (degraded to abstain): {router_stats['schema_violations']}",
            f"- abstain proposals: {router_stats['abstain_proposals']}",
        ]
    if provider_stats:
        cache = provider_stats.get("cache", {})
        hit_rate = cache.get("hit_rate")
        lines += [
            f"- calls: {provider_stats['calls']} "
            f"({provider_stats['network_calls']} reached the provider)",
            f"- cache hit rate: {'n/a' if hit_rate is None else _pct(hit_rate)} "
            f"({cache.get('hits', 0)} hits / {cache.get('misses', 0)} misses)",
            f"- mean latency: {provider_stats.get('mean_latency_seconds')}s per call",
        ]
    lines.append("")
    return lines


def render_calibration(report: Any) -> list[str]:
    lines = [
        "## Calibration of the router's probability estimates",
        "",
        "The plan is replaceable - a lookup table produces good plans. The probabilities "
        "are not: the economist layer cannot compute an expected value without them. So "
        "they are scored as forecasts, against a constant base rate and against a "
        "per-failure-code historical prior **fitted on the train split only** "
        f"({report.prior_fitted_on} transactions). Lower Brier is better.",
        "",
        "| estimate | n scored | base rate | router Brier | constant Brier | "
        "historical prior Brier | beats prior? |",
        "| --- | ---: | ---: | ---: | ---: | ---: | :---: |",
    ]
    for score in (report.act, report.abstain):
        if score.router_brier is None:
            lines.append(f"| `{score.label}` | 0 | - | - | - | - | - |")
            continue
        lines.append(
            f"| `{score.label}` | {score.n} | {score.base_rate:.4f} | "
            f"{score.router_brier:.4f} | {score.constant_brier:.4f} | "
            f"{score.historical_brier:.4f} | "
            f"{'**yes**' if score.beats_historical else 'no'} |"
        )
    lines.append("")
    for verdict in report.verdicts():
        lines.append(f"> {verdict}")
    lines.append("")

    for score in (report.act, report.abstain):
        if score.router_brier is None:
            continue
        lines += [
            f"### Reliability - `{score.label}`",
            "",
            "| predicted bin | n | mean predicted | observed frequency | gap |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        for b in score.reliability:
            row = b.to_dict()
            if row["count"] == 0:
                continue
            lines.append(
                f"| {row['bin']} | {row['count']} | {row['mean_predicted']:.3f} | "
                f"{row['observed_frequency']:.3f} | {row['gap']:+.3f} |"
            )
        lines.append("")
    return lines


def render_json(
    reports: Sequence[PolicyReport],
    *,
    split: str,
    n_txns: int,
    n_customers: int,
    seed: int,
    iterations: int,
    bootstrap_seed: int,
    ceilings: mx.Ceilings,
    clv_paise: int = CUSTOMER_LIFETIME_VALUE_PAISE,
    paired: Sequence[dict[str, Any]] | None = None,
    multiseed: Sequence[dict[str, Any]] | None = None,
    clv_sweep: Sequence[dict[str, Any]] | None = None,
    discount_sweep: Sequence[dict[str, Any]] | None = None,
    calibration: Any = None,
    provider: str | None = None,
    router_stats: dict[str, Any] | None = None,
    provider_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "split": split,
        "n_transactions": n_txns,
        "n_customers": n_customers,
        "seed": seed,
        "generator_hash": generator_hash(),
        "bootstrap": {"iterations": iterations, "seed": bootstrap_seed, "cluster": "customer_id"},
        "ceilings": ceilings.to_dict(),
        "cost_constants": cost_constants(),
        "clv_paise": clv_paise,
        "headline": build_headline(reports),
        "policies": [
            {
                "name": r.name,
                "is_reference_bound": r.is_reference_bound,
                "valid": r.valid,
                "compliance_violations": [v.describe() for v in r.result.violations[:20]],
                "metrics": r.metrics.to_dict(),
                "confidence_intervals": r.ci.to_dict(),
            }
            for r in reports
        ],
        "provider": provider,
        "router_stats": router_stats or {},
        "provider_stats": provider_stats or {},
        "calibration": calibration.to_dict() if calibration is not None else {},
        "paired_comparisons": list(paired) if paired else [],
        "multiseed": list(multiseed) if multiseed else [],
        "clv_sweep": {
            "rows": list(clv_sweep),
            "conclusion": sweep_conclusion(clv_sweep),
        }
        if clv_sweep
        else {},
        "discount_rate_sweep": list(discount_sweep) if discount_sweep else [],
    }


def print_stdout(
    reports: Sequence[PolicyReport], headline: str, ceilings: mx.Ceilings
) -> None:
    honest = [r for r in reports if not r.is_reference_bound]
    bounds = [r for r in reports if r.is_reference_bound]

    print()
    print("HEADLINE: " + headline)
    print(
        f"ceiling:  {_pct(ceilings.cap_limited)} recoverable within attempt caps "
        f"({_pct(ceilings.cap_free)} ignoring caps; caps cost "
        f"{ceilings.cost_of_caps_pp:.1f} pp)"
    )
    print()
    decisions = (
        f"{'policy':<31} {'status':<8} | {'precis':>7} {'recall':>7} {'F1':>7} "
        f"| {'capture':>7} {'sel.err':>7} | {'precis$':>7} {'recall$':>7} {'F1$':>7}"
    )
    print(decisions)
    print("-" * len(decisions))
    for report in reports:
        d, rd = report.metrics.decision, report.metrics.rupee_decision
        print(
            f"{report.name:<31} {report.status:<8} | {_rate(d.precision):>7} "
            f"{_rate(d.recall):>7} {_rate(d.f1):>7} "
            f"| {_rate(report.metrics.addressable_capture_rate):>7} "
            f"{_rate(report.metrics.action_selection_error_rate):>7} "
            f"| {_rate(rd.precision):>7} {_rate(rd.recall):>7} {_rate(rd.f1):>7}"
        )
    print()

    header = (
        f"{'policy':<31} {'status':<8} {'recovery':>9} {'uplift':>7} {'act%':>6} "
        f"{'abst':>5} {'restr.p':>8} {'net INR':>11} {'cost INR':>10} {'net value':>12} "
        f"{'INR/INR':>8} {'att':>5} {'viol':>5}"
    )
    print(header)
    print("-" * len(header))

    def emit(report: PolicyReport) -> None:
        m = report.metrics
        print(
            f"{report.name:<31} {report.status:<8} {_pct(m.recovery_rate):>9} "
            f"{m.net_uplift_pp:>+7.1f} {_pct(m.action_rate):>6} {m.n_abstained:>5} "
            f"{_rate(m.restraint_precision):>8} {_money(m.net_rupees):>11} "
            f"{_money(m.total_cost_rupees + m.annoyance_cost_rupees):>10} "
            f"{_money(m.net_value_rupees):>12} "
            f"{_ratio(m.cost_per_incremental_rupee, compact=True):>8} "
            f"{m.total_attempts:>5} {m.compliance_violations:>5}"
        )

    for report in honest:
        emit(report)
    if bounds:
        print()
        print("reference bounds (CHEAT - not results):")
        for report in bounds:
            emit(report)

    print()
    attrib = (
        f"{'policy':<31} | {'acted':>5} {'incr':>5} {'cannib':>6} {'wasted':>6} "
        f"{'hopeles':>7} {'wrongAct':>8} | {'abst':>5} {'restraint':>9} {'walkaway':>8} "
        f"{'missed':>6}"
    )
    print(attrib)
    print("-" * len(attrib))
    for report in reports:
        m = report.metrics
        print(
            f"{report.name:<31} | {m.n_acted:>5} {m.incremental.count:>5} "
            f"{m.cannibalised.count:>6} {m.wasted.count:>6} {m.futile_hopeless.count:>7} "
            f"{m.wrong_action.count:>8} | {m.n_abstained:>5} {m.correct_restraint.count:>9} "
            f"{m.correct_walkaway.count:>8} {m.missed_opportunity.count:>6}"
        )
    print()
    timing = (
        f"{'policy':<31} | {'recovery':>8} {'INR-wt':>8} | {'uplift':>7} {'INR-wt':>7} "
        f"| {'med days':>8} {'mean days':>9} {'<=72h':>7}"
    )
    print(timing)
    print("-" * len(timing))
    for report in reports:
        m = report.metrics
        print(
            f"{report.name:<31} | {_pct(m.recovery_rate):>8} {_pct(m.rupee_recovery_rate):>8} "
            f"| {m.net_uplift_pp:>+7.1f} {m.rupee_net_uplift_pp:>+7.1f} "
            f"| {_days(m.median_days_to_recovery, m):>8} {_days(m.mean_days_to_recovery, m):>9} "
            f"{_pct(m.recovered_within_72h_rate):>7}"
        )

    print()
    for report in reports:
        print(
            f"  {report.name}: decided {report.result.n} transactions in "
            f"{report.result.decide_seconds_total * 1000:.1f} ms"
        )


# ---------------------------------------------------------------------------
# multi-seed robustness
# ---------------------------------------------------------------------------


def run_multiseed(
    seeds: Sequence[int],
    policy_names: Sequence[str],
    split: str,
    *,
    n: int,
    n_customers: int,
    clv_paise: int = CUSTOMER_LIFETIME_VALUE_PAISE,
) -> list[dict[str, Any]]:
    """Regenerate the world at each seed and rerun every policy.

    Written into a temporary directory rather than over `data/generated`, so a
    robustness check never silently replaces the dataset the rest of the report
    was computed against.
    """
    collected: dict[str, list[mx.Metrics]] = {}
    violations: dict[str, int] = {}

    for seed in seeds:
        with tempfile.TemporaryDirectory(prefix=f"retry_econ_seed_{seed}_") as tmp:
            out = Path(tmp)
            generate(seed=seed, n=n, n_customers=n_customers, out_dir=out)
            transactions, store, splits = ensure_data(
                out, seed=seed, n=n, n_customers=n_customers
            )
            subset = filter_split(transactions, splits, split)
            for policy in build_policies(policy_names, store, subset):
                result = run(policy, subset, store, split=split)
                collected.setdefault(policy.name, []).append(
                    mx.compute_for_run(result, clv_paise=clv_paise)
                )
                violations[policy.name] = violations.get(policy.name, 0) + len(result.violations)

    rows: list[dict[str, Any]] = []
    for name, runs in collected.items():
        recoveries = [m.recovery_rate for m in runs]
        uplifts = [m.net_uplift_pp for m in runs]
        nets = [m.net_rupees for m in runs]
        attempts = [m.attempts_per_txn for m in runs]
        precisions = [m.restraint_precision for m in runs if m.restraint_precision is not None]
        rows.append(
            {
                "policy": name,
                "n_seeds": len(runs),
                "seeds": list(seeds),
                "recovery_rate_mean": sum(recoveries) / len(recoveries),
                "recovery_rate_min": min(recoveries),
                "recovery_rate_max": max(recoveries),
                "net_uplift_pp_mean": sum(uplifts) / len(uplifts),
                "net_uplift_pp_min": min(uplifts),
                "net_uplift_pp_max": max(uplifts),
                "net_rupees_mean": sum(nets) / len(nets),
                "net_rupees_min": min(nets),
                "net_rupees_max": max(nets),
                "attempts_per_txn_mean": sum(attempts) / len(attempts),
                "restraint_precision_mean": (
                    sum(precisions) / len(precisions) if precisions else None
                ),
                "violations_total": violations[name],
            }
        )
    return rows


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="retry_economist.eval.cli",
        description="Score recovery policies against held-out failed payments.",
    )
    parser.add_argument("--split", default="holdout", choices=("holdout", "train", "all"))
    parser.add_argument(
        "--policies",
        default="do_nothing,naive_retry_3x,rules_only,oracle_best",
        help="comma-separated policy names (default: all four)",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="data seed (default: 42)")
    parser.add_argument(
        "--seeds",
        default=None,
        help="comma-separated seeds; regenerates the world at each and reports mean +/- range",
    )
    parser.add_argument("--n", type=int, default=DEFAULT_N)
    parser.add_argument("--customers", type=int, default=DEFAULT_CUSTOMERS)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="score only the first N transactions of the split - use for smoke tests "
        "before spending API quota on the whole holdout",
    )
    parser.add_argument(
        "--mock-llm",
        action="store_true",
        help="force the deterministic stand-in even if a key and pinned model exist",
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--iterations", type=int, default=bs.DEFAULT_ITERATIONS)
    parser.add_argument("--bootstrap-seed", type=int, default=bs.DEFAULT_SEED)
    parser.add_argument(
        "--clv-sweep",
        action="store_true",
        help="re-price every policy at CLV 4,000 / 12,000 / 30,000 INR and report what moves",
    )
    parser.add_argument(
        "--discount-sweep",
        action="store_true",
        help=(
            f"re-DECIDE {RETRY_ECONOMIST_PRIOR_NAME!r} at daily discount rates "
            f"{DISCOUNT_RATE_SWEEP} and report whether its advantage survives"
        ),
    )
    parser.add_argument(
        "--discount-rate",
        type=float,
        default=DAILY_DISCOUNT_RATE,
        help=f"daily discount rate for {RETRY_ECONOMIST_PRIOR_NAME!r} (default {DAILY_DISCOUNT_RATE})",
    )
    args = parser.parse_args(argv)

    policy_names = [p.strip() for p in args.policies.split(",") if p.strip()]
    if not policy_names:
        raise SystemExit("--policies must name at least one policy")

    transactions, store, splits = ensure_data(
        args.data_dir, seed=args.seed, n=args.n, n_customers=args.customers
    )
    subset = filter_split(transactions, splits, args.split)
    if not subset:
        raise SystemExit(f"split {args.split!r} contains no transactions")
    if args.limit is not None:
        # Sorted by time already, so a limit takes a contiguous slice of the
        # feed rather than a random sample - the signals depend on neighbours.
        subset = subset[: args.limit]
        print(f"NOTE: --limit {args.limit} in effect; scoring {len(subset)} transactions only")
    _ROUTER_CONTEXT["prefer_real"] = not args.mock_llm

    # The prior is fitted on TRAIN only, once, and reused everywhere a
    # historical prior is needed this run (the combined policy, and the
    # router's own calibration report below). Fitting it on the split being
    # scored would let it see the outcomes it is being graded against.
    train = filter_split(transactions, splits, "train")
    fitted_prior: cal.HistoricalPrior | None = None
    needs_prior = any(name in policy_names for name in PRIOR_BASED_POLICIES) or (
        "llm_router_only" in policy_names
    )
    if needs_prior:
        fitted_prior = cal.HistoricalPrior.fit(train, store)
    if any(name in policy_names for name in PRIOR_BASED_POLICIES):
        _PRIOR_CONTEXT["estimator"] = historical_prior_estimator(fitted_prior)
        _PRIOR_CONTEXT["daily_discount_rate"] = args.discount_rate
        _PRIOR_CONTEXT["prior"] = fitted_prior

    reports = evaluate(
        policy_names,
        subset,
        store,
        args.split,
        iterations=args.iterations,
        bootstrap_seed=args.bootstrap_seed,
    )

    multiseed = None
    if args.seeds:
        seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
        multiseed = run_multiseed(
            seeds, policy_names, args.split, n=args.n, n_customers=args.customers
        )

    paired = run_paired_comparisons(
        reports, iterations=args.iterations, seed=args.bootstrap_seed
    )
    clv_sweep = run_clv_sweep(reports) if args.clv_sweep else None
    discount_sweep = (
        run_discount_rate_sweep(subset, store, fitted_prior, args.split, reports=reports)
        if args.discount_sweep and fitted_prior is not None
        else None
    )

    calibration = None
    policy = _ROUTER_CONTEXT.get("policy")
    if policy is not None and policy.proposals:
        prior = fitted_prior if fitted_prior is not None else cal.HistoricalPrior.fit(train, store)
        calibration = cal.evaluate(cal.build_records(policy.proposals, subset, store), prior)

    common = {
        "split": args.split,
        "n_txns": len(subset),
        "n_customers": len({t.customer_id for t in subset}),
        "seed": args.seed,
        "iterations": args.iterations,
        "bootstrap_seed": args.bootstrap_seed,
        "ceilings": mx.ceilings(subset, store),
        "paired": paired,
        "calibration": calibration,
        "provider": _ROUTER_CONTEXT.get("label"),
        "router_stats": (
            _ROUTER_CONTEXT["router"].stats.to_dict() if "router" in _ROUTER_CONTEXT else None
        ),
        "provider_stats": (
            _ROUTER_CONTEXT["provider"].report() if "provider" in _ROUTER_CONTEXT else None
        ),
        "multiseed": multiseed,
        "clv_sweep": clv_sweep,
        "discount_sweep": discount_sweep,
    }

    args.out.mkdir(parents=True, exist_ok=True)
    md_path = args.out / f"{args.split}_scoreboard.md"
    json_path = args.out / f"{args.split}_scoreboard.json"
    with md_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_markdown(reports, **common))
    with json_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(render_json(reports, **common), indent=2) + "\n")

    print_stdout(reports, build_headline(reports), common["ceilings"])
    if paired:
        print("paired comparisons (same customers resampled for both arms):")
        for row in paired:
            u, f = row["net_uplift_pp_delta"], row["decision_f1_delta"]
            print(
                f"  {row['subject']} vs {row['baseline']}: "
                f"uplift {u['point']:+.2f} pp [{u['low']:+.2f}, {u['high']:+.2f}]"
                f"{' *' if row['uplift_significant'] else ''}  |  "
                f"F1 {f['point']:+.4f} [{f['low']:+.4f}, {f['high']:+.4f}]"
                f"{' *' if row['f1_significant'] else ''}"
            )
        print("  (* = interval excludes zero)")
        print()

    if multiseed:
        print("robustness across seeds:")
        for row in multiseed:
            print(
                f"  {row['policy']:<31} recovery {_pct(row['recovery_rate_mean'])} "
                f"[{_pct(row['recovery_rate_min'])}, {_pct(row['recovery_rate_max'])}]  "
                f"uplift {row['net_uplift_pp_mean']:+.1f} pp"
            )
        print()
    if clv_sweep:
        print("CLV sensitivity:")
        for row in clv_sweep:
            values = "  ".join(
                f"INR{p['clv_rupees'] / 1000:.0f}k: {_money(p['net_value_rupees'])}"
                for p in row["points"]
            )
            print(f"  {row['policy']:<31} net value  {values}")
        print(f"  => {sweep_conclusion(clv_sweep)}")
        print()
    if discount_sweep:
        print("daily discount rate sensitivity (RE-DECIDED, not re-priced):")
        for row in discount_sweep:
            values = "  ".join(
                f"{p['daily_discount_rate']:.3f}: uplift {p['net_uplift_pp']:+.2f}pp"
                for p in row["points"]
            )
            print(f"  {row['policy']:<31} {values}")
            for baseline, info in row["advantage_over"].items():
                mark = "survives" if info["advantage_survives_all_rates"] else "DOES NOT survive"
                print(f"    vs {baseline}: advantage {mark} every rate tested")
        print()
    if "router" in _ROUTER_CONTEXT:
        router_stats = _ROUTER_CONTEXT["router"].stats.to_dict()
        provider_report = _ROUTER_CONTEXT["provider"].report()
        cache = provider_report["cache"]
        print(f"router: provider={_ROUTER_CONTEXT['label']}")
        print(
            f"  proposals={router_stats['proposals']} "
            f"parse_failures={router_stats['parse_failures']} "
            f"schema_violations={router_stats['schema_violations']} "
            f"abstains={router_stats['abstain_proposals']}"
        )
        print(
            f"  calls={provider_report['calls']} network={provider_report['network_calls']} "
            f"cache_hits={cache['hits']} cache_misses={cache['misses']} "
            f"hit_rate={'n/a' if cache['hit_rate'] is None else _pct(cache['hit_rate'])} "
            f"mean_latency={provider_report['mean_latency_seconds']}s"
        )
        print()

    if calibration is not None:
        print("calibration (Brier, lower is better):")
        for score in (calibration.act, calibration.abstain):
            if score.router_brier is None:
                print(f"  {score.label:<22} no scored transactions")
                continue
            print(
                f"  {score.label:<22} n={score.n:<5} router={score.router_brier:.4f}  "
                f"constant={score.constant_brier:.4f}  "
                f"prior(train)={score.historical_brier:.4f}  "
                f"{'BEATS prior' if score.beats_historical else 'does NOT beat prior'}"
            )
        print()

    print(f"wrote {md_path}")
    print(f"wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
