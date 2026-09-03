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
from retry_economist.eval import metrics as mx
from retry_economist.eval.costs import CUSTOMER_LIFETIME_VALUE_PAISE, cost_constants
from retry_economist.eval.simulator import (
    RunResult,
    filter_split,
    load_counterfactuals,
    load_observed,
    run,
)
from retry_economist.generator.cli import generate
from retry_economist.policies.base import ObservedTransaction, Policy
from retry_economist.policies.do_nothing import DoNothingPolicy
from retry_economist.policies.oracle_best import OracleBestPolicy

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

#: Named constructors, so `--policies` takes short names and the counterfactual
#: store is injected explicitly into anything that needs it.
POLICY_BUILDERS: dict[str, Callable[[Mapping[str, Any]], Policy]] = {
    "do_nothing": lambda store: DoNothingPolicy(),
    "oracle_best": lambda store: OracleBestPolicy(_outcomes_by_txn(store)),
}


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


def _days(value: float | None) -> str:
    """None means nothing recovered, which is not the same as recovering in 0 days."""
    return "n/a" if value is None else f"{value:.2f}"


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


def build_policies(names: Sequence[str], store: Mapping[str, Any]) -> list[Policy]:
    policies: list[Policy] = []
    for name in names:
        if name not in POLICY_BUILDERS:
            raise SystemExit(
                f"unknown policy {name!r}; available: {', '.join(sorted(POLICY_BUILDERS))}"
            )
        policies.append(POLICY_BUILDERS[name](store))
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
    for policy in build_policies(policy_names, store):
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

    # The baseline is whichever honest policy spends the fewest attempts -
    # normally abstaining, but never assumed to be.
    comparison = min(others, key=lambda r: r.metrics.attempts_per_txn)

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


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

_RESULTS_HEADER = (
    "| policy | status | recovery rate (95% CI) | net uplift pp (95% CI) | "
    "recovery (INR-wt) | uplift pp (INR-wt) | median days | mean days | recovered <=72h | "
    "action rate | abstained | restraint precision | net INR | cost INR | net value INR | "
    "INR spent per INR earned | attempts | contact | viol |"
)
_RESULTS_RULE = "| --- | --- | --- | --- |" + " ---: |" * 15

_ATTRIB_HEADER = (
    "| policy | acted | incremental | cannibalised | wasted | futile | abstained | "
    "correct restraint | correct walkaway | missed opportunity | sum |"
)
_ATTRIB_RULE = "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"


def _results_row(report: PolicyReport) -> str:
    m = report.metrics
    return (
        f"| `{report.name}` | {report.status} | {_ci_pct(report.ci.recovery_rate)} | "
        f"{_ci_pp(report.ci.net_uplift_pp)} | {_pct(m.rupee_recovery_rate)} | "
        f"{m.rupee_net_uplift_pp:+.1f} | {_days(m.median_days_to_recovery)} | "
        f"{_days(m.mean_days_to_recovery)} | {_pct(m.recovered_within_72h_rate)} | "
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
        f"{m.wasted.count} | {m.futile.count} | {m.n_abstained} | {m.correct_restraint.count} | "
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
        "| failure code | n | recovery | organic | uplift pp | recovery (INR-wt) | "
        "uplift pp (INR-wt) | median days | mean days | recovered <=72h | incr | cannib | "
        "missed | restraint precision | net INR | attempts |",
        "| --- | ---: |" + " ---: |" * 14,
    ]
    for code, m in sorted(
        report.metrics.per_failure_code.items(), key=lambda kv: (-kv[1].n, kv[0])
    ):
        lines.append(
            f"| `{code}` | {m.n} | {_pct(m.recovery_rate)} | {_pct(m.organic_rate)} | "
            f"{m.net_uplift_pp:+.1f} | {_pct(m.rupee_recovery_rate)} | "
            f"{m.rupee_net_uplift_pp:+.1f} | {_days(m.median_days_to_recovery)} | "
            f"{_days(m.mean_days_to_recovery)} | {_pct(m.recovered_within_72h_rate)} | "
            f"{m.incremental.count} | {m.cannibalised.count} | "
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
    multiseed: Sequence[dict[str, Any]] | None = None,
    clv_sweep: Sequence[dict[str, Any]] | None = None,
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
        "those two buckets.",
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

    lines += ["## Breakdown by failure code", ""]
    for report in reports:
        lines += _per_mode_table(report)

    if multiseed:
        lines += render_multiseed(multiseed)

    if clv_sweep:
        lines += render_clv_sweep(clv_sweep)

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
    multiseed: Sequence[dict[str, Any]] | None = None,
    clv_sweep: Sequence[dict[str, Any]] | None = None,
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
        "multiseed": list(multiseed) if multiseed else [],
        "clv_sweep": {
            "rows": list(clv_sweep),
            "conclusion": sweep_conclusion(clv_sweep),
        }
        if clv_sweep
        else {},
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
    header = (
        f"{'policy':<24} {'status':<8} {'recovery':>9} {'uplift':>7} {'act%':>6} "
        f"{'abst':>5} {'restr.p':>8} {'net INR':>11} {'cost INR':>10} {'net value':>12} "
        f"{'INR/INR':>8} {'att':>5} {'viol':>5}"
    )
    print(header)
    print("-" * len(header))

    def emit(report: PolicyReport) -> None:
        m = report.metrics
        print(
            f"{report.name:<24} {report.status:<8} {_pct(m.recovery_rate):>9} "
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
        f"{'policy':<24} | {'acted':>5} {'incr':>5} {'cannib':>6} {'wasted':>6} {'futile':>6} "
        f"| {'abst':>5} {'restraint':>9} {'walkaway':>8} {'missed':>6}"
    )
    print(attrib)
    print("-" * len(attrib))
    for report in reports:
        m = report.metrics
        print(
            f"{report.name:<24} | {m.n_acted:>5} {m.incremental.count:>5} "
            f"{m.cannibalised.count:>6} {m.wasted.count:>6} {m.futile.count:>6} "
            f"| {m.n_abstained:>5} {m.correct_restraint.count:>9} "
            f"{m.correct_walkaway.count:>8} {m.missed_opportunity.count:>6}"
        )
    print()
    timing = (
        f"{'policy':<24} | {'recovery':>8} {'INR-wt':>8} | {'uplift':>7} {'INR-wt':>7} "
        f"| {'med days':>8} {'mean days':>9} {'<=72h':>7}"
    )
    print(timing)
    print("-" * len(timing))
    for report in reports:
        m = report.metrics
        print(
            f"{report.name:<24} | {_pct(m.recovery_rate):>8} {_pct(m.rupee_recovery_rate):>8} "
            f"| {m.net_uplift_pp:>+7.1f} {m.rupee_net_uplift_pp:>+7.1f} "
            f"| {_days(m.median_days_to_recovery):>8} {_days(m.mean_days_to_recovery):>9} "
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
            for policy in build_policies(policy_names, store):
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
        default="do_nothing,oracle_best",
        help="comma-separated policy names (default: do_nothing,oracle_best)",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="data seed (default: 42)")
    parser.add_argument(
        "--seeds",
        default=None,
        help="comma-separated seeds; regenerates the world at each and reports mean +/- range",
    )
    parser.add_argument("--n", type=int, default=DEFAULT_N)
    parser.add_argument("--customers", type=int, default=DEFAULT_CUSTOMERS)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--iterations", type=int, default=bs.DEFAULT_ITERATIONS)
    parser.add_argument("--bootstrap-seed", type=int, default=bs.DEFAULT_SEED)
    parser.add_argument(
        "--clv-sweep",
        action="store_true",
        help="re-price every policy at CLV 4,000 / 12,000 / 30,000 INR and report what moves",
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

    clv_sweep = run_clv_sweep(reports) if args.clv_sweep else None

    common = {
        "split": args.split,
        "n_txns": len(subset),
        "n_customers": len({t.customer_id for t in subset}),
        "seed": args.seed,
        "iterations": args.iterations,
        "bootstrap_seed": args.bootstrap_seed,
        "ceilings": mx.ceilings(subset, store),
        "multiseed": multiseed,
        "clv_sweep": clv_sweep,
    }

    args.out.mkdir(parents=True, exist_ok=True)
    md_path = args.out / f"{args.split}_scoreboard.md"
    json_path = args.out / f"{args.split}_scoreboard.json"
    with md_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_markdown(reports, **common))
    with json_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(render_json(reports, **common), indent=2) + "\n")

    print_stdout(reports, build_headline(reports), common["ceilings"])
    if multiseed:
        print("robustness across seeds:")
        for row in multiseed:
            print(
                f"  {row['policy']:<24} recovery {_pct(row['recovery_rate_mean'])} "
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
            print(f"  {row['policy']:<24} net value  {values}")
        print(f"  => {sweep_conclusion(clv_sweep)}")
        print()
    print(f"wrote {md_path}")
    print(f"wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
