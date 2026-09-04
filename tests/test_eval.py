"""Contract tests for the evaluation harness.

The harness exists to produce numbers people will act on, so these tests guard
the things that would make those numbers lies rather than the arithmetic that
would make them wrong:

- the abstain baseline reproduces the generator's own organic rate, so the
  simulator and the world it scores agree on what "no intervention" means;
- the cheating bound reproduces the generator's own ceiling, so "how much was
  available to win" means the same thing in both places;
- the attribution buckets are exhaustive, so no recovery can go unclassified;
- a policy cannot exceed an invoice's debit-attempt cap without being caught
  and marked INVALID;
- and no policy can see the counterfactual answers, enforced by walking the
  syntax tree rather than by trusting a code review.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from retry_economist.eval import bootstrap as bs  # noqa: E402
from retry_economist.eval import metrics as mx  # noqa: E402
from retry_economist.eval.simulator import (  # noqa: E402
    filter_split,
    load_counterfactuals,
    load_observed,
    run,
)
from retry_economist.generator.cli import generate  # noqa: E402
from retry_economist.policies.base import (  # noqa: E402
    Decision,
    InvalidPlan,
    ObservedTransaction,
)
from retry_economist.policies.do_nothing import DoNothingPolicy  # noqa: E402
from retry_economist.policies.oracle_best import OracleBestPolicy  # noqa: E402

SEED = 42

#: Both the small development scale and the canonical dataset scale. Thresholds
#: are asserted at both, so nothing here is fitted to one dataset size.
SCALES = ((800, 300), (2500, 900))

#: Modules that may read counterfactual data. Exactly one, and it is the
#: deliberate upper bound: it is named "(CHEATS)", takes its data source as a
#: constructor argument, and the scoreboard prints it in a quarantined section.
LEAKAGE_ALLOWLIST = {"oracle_best.py"}

#: Directories whose contents must stay blind to the counterfactual store.
#: `router` and `economist` do not exist yet; they are listed now so the guard
#: covers them the moment Phase 3 creates them.
GUARDED_PACKAGES = ("policies", "router", "economist")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", params=SCALES, ids=lambda s: f"n{s[0]}_c{s[1]}")
def dataset(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> dict:
    """A freshly generated dataset plus its own summary report, at each scale.

    Generated here rather than read from `data/generated` so the summary numbers
    the tests compare against were provably produced by the same run as the
    transactions.
    """
    n, n_customers = request.param
    out = tmp_path_factory.mktemp(f"eval_data_{n}")
    generate(seed=SEED, n=n, n_customers=n_customers, out_dir=out)
    transactions = load_observed(out / "observed.jsonl")
    store = load_counterfactuals(out / "oracle.jsonl")
    splits = json.loads((out / "splits.json").read_text(encoding="utf-8"))
    return {
        "n": n,
        "n_customers": n_customers,
        "dir": out,
        "transactions": transactions,
        "store": store,
        "splits": splits,
        "summary": (out / "summary.md").read_text(encoding="utf-8"),
        "outcomes_by_txn": {tid: row["outcomes"] for tid, row in store.items()},
    }


def _summary_pct(summary: str, label: str) -> float:
    """Pull a percentage out of the generator's own summary table."""
    for line in summary.splitlines():
        if line.startswith("|") and label in line:
            match = re.search(r"([\d.]+)%", line)
            if match:
                return float(match.group(1)) / 100.0
    raise AssertionError(f"no percentage for {label!r} in summary.md")


def _run(policy, dataset: dict, split: str = "all"):
    subset = filter_split(dataset["transactions"], dataset["splits"], split)
    return run(policy, subset, dataset["store"], split=split)


# ---------------------------------------------------------------------------
# the abstain baseline
# ---------------------------------------------------------------------------


def test_do_nothing_matches_the_generators_organic_rate(dataset: dict) -> None:
    """The simulator's "no intervention" must be the generator's own.

    Compared over the full population, since the summary reports over all
    transactions while an evaluation normally runs on one split.
    """
    result = _run(DoNothingPolicy(), dataset, split="all")
    metrics = mx.compute_for_run(result)
    expected = _summary_pct(dataset["summary"], "organic recovery")

    assert round(metrics.recovery_rate, 3) == round(expected, 3), (
        f"abstain recovered {metrics.recovery_rate:.4f}; summary.md says {expected:.4f}"
    )


def test_do_nothing_is_free_and_gains_nothing(dataset: dict) -> None:
    result = _run(DoNothingPolicy(), dataset, split="all")
    metrics = mx.compute_for_run(result)

    assert metrics.net_uplift_pp == 0.0
    assert metrics.total_cost_rupees == 0.0
    assert metrics.annoyance_cost_rupees == 0.0
    assert metrics.total_attempts == 0
    assert metrics.contact_rate == 0.0
    assert metrics.compliance_violations == 0
    # Abstaining can neither create nor destroy revenue.
    assert metrics.incremental.count == 0
    assert metrics.cannibalised.count == 0


# ---------------------------------------------------------------------------
# the cheating bound
# ---------------------------------------------------------------------------


def test_ceiling_matches_the_generators_summary(dataset: dict) -> None:
    """The harness must reproduce the generator's own stated ceiling exactly.

    `summary.md` computes "any action recovers", taking no view on whether the
    invoice had a debit attempt left to pay for that action, so the comparable
    figure is the cap-free ceiling.
    """
    computed = mx.ceilings(dataset["transactions"], dataset["store"])
    expected = _summary_pct(dataset["summary"], "oracle-best policy")

    assert round(computed.cap_free, 3) == round(expected, 3), (
        f"cap-free ceiling {computed.cap_free:.4f}; summary.md says {expected:.4f}"
    )


def test_oracle_best_reaches_the_compliant_ceiling(dataset: dict) -> None:
    """The cheating bound recovers everything the attempt caps allow, and no more.

    It cannot reach the cap-free ceiling, and neither can anything else: the
    compliance gate truncates a plan that spends attempts the invoice does not
    have, so a policy reaching for those actions scores below the bound rather
    than above it. The difference between the two ceilings is recovery that
    scheme rules cost the merchant - not headroom a better router could take.
    """
    result = _run(OracleBestPolicy(dataset["outcomes_by_txn"]), dataset, split="all")
    metrics = mx.compute_for_run(result)
    computed = mx.ceilings(dataset["transactions"], dataset["store"])

    assert result.is_valid, "the bound must never breach an attempt cap"
    assert metrics.recovery_rate == pytest.approx(computed.cap_limited)
    assert metrics.recovery_rate <= computed.cap_free
    assert computed.cost_of_caps_pp > 0, "this dataset should contain cap-bound transactions"
    # It also never destroys revenue: abstaining is available whenever acting
    # would not have worked.
    assert metrics.cannibalised.count == 0


# ---------------------------------------------------------------------------
# the compliance gate
# ---------------------------------------------------------------------------


class _CapBustingPolicy:
    """Asks for six debit attempts on every invoice; no cap is that generous."""

    name = "cap_buster"

    def decide(self, txn: ObservedTransaction) -> Decision:
        return Decision(
            plan=[
                "retry_now",
                "retry_in_2h",
                "retry_in_24h",
                "retry_next_salary_day",
                "nudge_then_retry",
                "switch_to_upi_intent",
            ],
            reason="deliberately exceeds the attempt cap",
        )


class _UnknownActionPolicy:
    name = "unknown_action"

    def decide(self, txn: ObservedTransaction) -> Decision:
        return Decision(plan=["reboot_the_issuer"], reason="not a real action")


def test_exceeding_the_attempt_cap_is_caught_and_marked_invalid(dataset: dict) -> None:
    result = _run(_CapBustingPolicy(), dataset, split="all")
    metrics = mx.compute_for_run(result)

    assert metrics.compliance_violations > 0
    assert not result.is_valid, "a policy that breaches the cap must not be scored as valid"
    # Truncated, not obeyed: nothing may execute more attempts than it had.
    for outcome in result.outcomes:
        txn = next(t for t in dataset["transactions"] if t.txn_id == outcome.txn_id)
        assert outcome.attempts_used <= txn.attempts_left


def test_unknown_action_raises_invalid_plan(dataset: dict) -> None:
    with pytest.raises(InvalidPlan, match="unknown action"):
        _run(_UnknownActionPolicy(), dataset, split="all")


def test_null_action_may_not_appear_inside_a_plan() -> None:
    """Doing nothing is the empty plan, and only the empty plan."""
    from retry_economist.policies.base import validate_plan

    assert validate_plan([]) == ()
    with pytest.raises(InvalidPlan, match="may not appear in a plan"):
        validate_plan(["do_nothing"])
    with pytest.raises(InvalidPlan, match="may not appear in a plan"):
        validate_plan(["retry_now", "do_nothing"])


# ---------------------------------------------------------------------------
# attribution
# ---------------------------------------------------------------------------


def test_attribution_buckets_are_exhaustive(dataset: dict) -> None:
    """All seven buckets, acted and abstained together, sum to n."""
    policies = [
        DoNothingPolicy(),
        OracleBestPolicy(dataset["outcomes_by_txn"]),
        _CapBustingPolicy(),
    ]
    for policy in policies:
        result = _run(policy, dataset, split="all")
        m = mx.compute_for_run(result)
        buckets = (
            m.incremental,
            m.cannibalised,
            m.wasted,
            m.futile,
            m.correct_restraint,
            m.correct_walkaway,
            m.missed_opportunity,
        )
        total = sum(b.count for b in buckets)
        assert total == m.n == dataset["n"], f"{policy.name}: buckets sum to {total}, not {m.n}"
        assert sum(b.share for b in buckets) == pytest.approx(1.0)
        # The two halves must also partition on the acted/abstained split.
        assert m.incremental.count + m.cannibalised.count + m.wasted.count + m.futile.count == (
            m.n_acted
        )
        assert (
            m.correct_restraint.count + m.correct_walkaway.count + m.missed_opportunity.count
            == m.n_abstained
        )
        assert m.n_acted + m.n_abstained == m.n
        for code, sub in m.per_failure_code.items():
            sub_total = sum(
                b.count
                for b in (
                    sub.incremental,
                    sub.cannibalised,
                    sub.wasted,
                    sub.futile,
                    sub.correct_restraint,
                    sub.correct_walkaway,
                    sub.missed_opportunity,
                )
            )
            assert sub_total == sub.n, f"{policy.name}/{code}: buckets sum to {sub_total}"


def test_restraint_is_not_scored_as_waste(dataset: dict) -> None:
    """Abstaining must never land in an ACTED bucket.

    This is the correction the seven-bucket scheme exists for: under the old
    four-bucket scheme, a customer left alone who paid unaided was counted as
    "wasted" - spend for nothing - despite nothing having been spent.
    """
    result = _run(DoNothingPolicy(), dataset, split="all")
    m = mx.compute_for_run(result)

    assert m.n_acted == 0 and m.action_rate == 0.0
    assert m.n_abstained == m.n
    for bucket in (m.incremental, m.cannibalised, m.wasted, m.futile):
        assert bucket.count == 0, "abstaining cannot populate an acted bucket"
    # Everyone who paid unaided is correct restraint, by definition.
    assert m.correct_restraint.count == sum(o.would_pay_anyway for o in result.outcomes)


def test_restraint_precision_is_a_real_rate(dataset: dict) -> None:
    """Restraint precision counts untouched transactions nothing could improve."""
    result = _run(DoNothingPolicy(), dataset, split="all")
    m = mx.compute_for_run(result)

    assert m.restraint_precision is not None
    assert m.restraint_precision == pytest.approx(
        (m.correct_restraint.count + m.correct_walkaway.count) / m.n_abstained
    )
    assert 0.0 < m.restraint_precision < 1.0, "a degenerate rate would prove nothing"
    # Abstaining on everything means every recoverable transaction is a miss, so
    # the complement must be exactly the missed-opportunity share.
    assert m.missed_opportunity.count == m.n_abstained - (
        m.correct_restraint.count + m.correct_walkaway.count
    )

    # A policy that acts everywhere it can never abstains, so the rate is
    # undefined rather than zero - and must be reported as such.
    everywhere = mx.compute_for_run(_run(_CapBustingPolicy(), dataset, split="all"))
    if everywhere.n_abstained == 0:
        assert everywhere.restraint_precision is None


# ---------------------------------------------------------------------------
# the isolation rule
# ---------------------------------------------------------------------------


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    """Identify docstring constants so prose is not mistaken for a data path."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _leakage_findings(path: Path) -> list[str]:
    """Every way this module could reach the counterfactual store.

    Works on the syntax tree, not on text, so the rule cannot be satisfied by
    wording: comments never reach the AST at all, and docstrings are excluded
    deliberately, which is what lets `base.py` document the rule it enforces.
    Identifiers, imports and any other string literal are all checked.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = _docstring_node_ids(tree)
    findings: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("retry_economist.eval"):
                    findings.append(f"imports {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("retry_economist.eval"):
                findings.append(f"imports from {module}")
            if "oracle" in module.lower():
                findings.append(f"imports from {module}")
            for alias in node.names:
                if "oracle" in alias.name.lower():
                    findings.append(f"imports name {alias.name}")
        elif isinstance(node, ast.Name):
            if "oracle" in node.id.lower():
                findings.append(f"references identifier {node.id}")
        elif isinstance(node, ast.Attribute):
            if "oracle" in node.attr.lower():
                findings.append(f"references attribute .{node.attr}")
        elif isinstance(node, ast.arg):
            if "oracle" in node.arg.lower():
                findings.append(f"takes a parameter named {node.arg}")
        elif isinstance(node, ast.keyword):
            if node.arg and "oracle" in node.arg.lower():
                findings.append(f"passes keyword {node.arg}")
        elif isinstance(node, ast.Constant):
            if (
                isinstance(node.value, str)
                and "oracle" in node.value.lower()
                and id(node) not in docstrings
            ):
                findings.append(f"contains the string literal {node.value!r}")

    return findings


def test_no_policy_can_reach_the_counterfactual_store() -> None:
    """Walk the AST of every guarded module and fail on any route to the answers.

    A leak here would not crash anything. It would simply produce an excellent
    scoreboard, which is exactly why this is a test and not a convention.
    """
    package_root = SRC / "retry_economist"
    checked = 0
    for package in GUARDED_PACKAGES:
        directory = package_root / package
        if not directory.exists():
            continue  # not built yet; the guard covers it as soon as it is
        for path in sorted(directory.rglob("*.py")):
            checked += 1
            findings = _leakage_findings(path)
            if path.name in LEAKAGE_ALLOWLIST:
                assert findings, (
                    f"{path.name} is allow-listed as the deliberate cheating bound but no "
                    "longer reads counterfactual data; remove it from the allow-list"
                )
                continue
            assert not findings, f"{path.relative_to(SRC)} leaks: {findings}"

    assert checked >= 4, "leakage guard found almost nothing to check; is the path right?"


def test_observed_transaction_rejects_a_counterfactual_row(dataset: dict) -> None:
    """The policy input type refuses anything richer than the observed feed."""
    counterfactual_row = next(iter(dataset["store"].values()))
    with pytest.raises(InvalidPlan, match="not an observed transaction"):
        ObservedTransaction.from_row(counterfactual_row)


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------


def test_bootstrap_brackets_its_point_estimate(dataset: dict) -> None:
    policy = OracleBestPolicy(dataset["outcomes_by_txn"])
    result = _run(policy, dataset, split="holdout")
    metrics = mx.compute_for_run(result)
    ci = bs.bootstrap_run(result, iterations=500, seed=7)

    assert ci.recovery_rate.point == pytest.approx(metrics.recovery_rate)
    assert ci.recovery_rate.contains(metrics.recovery_rate)
    assert ci.net_uplift_pp.contains(metrics.net_uplift_pp)
    assert ci.recovery_rate.low < ci.recovery_rate.high, "a degenerate interval hides variance"
    # Clustered on customers, so the interval covers whole payers, not rows.
    assert ci.n_customers == len({o.customer_id for o in result.outcomes})


def test_bootstrap_is_reproducible(dataset: dict) -> None:
    policy = OracleBestPolicy(dataset["outcomes_by_txn"])
    result = _run(policy, dataset, split="holdout")

    first = bs.bootstrap_run(result, iterations=500, seed=99)
    second = bs.bootstrap_run(result, iterations=500, seed=99)
    assert first.to_dict() == second.to_dict()

    different = bs.bootstrap_run(result, iterations=500, seed=100)
    assert different.to_dict() != first.to_dict(), "different seeds should not coincide exactly"


def test_paired_bootstrap_is_tighter_than_comparing_two_intervals(dataset: dict) -> None:
    """The paired interval should be narrower than the marginals it compares.

    This is the reason the paired form exists: shared customer-composition noise
    cancels inside each iteration instead of being counted in both intervals.
    """
    abstain = _run(DoNothingPolicy(), dataset, split="holdout")
    bound = _run(OracleBestPolicy(dataset["outcomes_by_txn"]), dataset, split="holdout")

    paired = bs.paired_bootstrap(bound, abstain, iterations=500, seed=11)
    marginal = bs.bootstrap_run(bound, iterations=500, seed=11).net_uplift_pp

    assert paired.point is not None and paired.point > 0
    assert paired.contains(paired.point)
    paired_width = paired.high - paired.low
    marginal_width = marginal.high - marginal.low
    assert paired_width <= marginal_width * 1.05, (
        f"paired width {paired_width:.2f} should not exceed marginal width {marginal_width:.2f}"
    )


# ---------------------------------------------------------------------------
# the headline generator
# ---------------------------------------------------------------------------


def _report_for(policy, dataset: dict, split: str = "all"):
    """A PolicyReport, so headline logic can be tested on real runs."""
    from retry_economist.eval.cli import PolicyReport

    result = _run(policy, dataset, split=split)
    return PolicyReport(
        name=policy.name,
        is_reference_bound=result.is_reference_bound,
        metrics=mx.compute_for_run(result),
        ci=bs.bootstrap_run(result, iterations=100, seed=3),
        result=result,
    )


def test_headline_refuses_to_compare_against_a_cheating_bound(dataset: dict) -> None:
    """With only one honest policy, there is no claim to make - so make none.

    The previous generator emitted "Recovered 19.2% vs 59.6% ... using 100%
    fewer retry attempts", comparing abstention to a policy that had read the
    answers. Every clause of that was misleading.
    """
    from retry_economist.eval.cli import NO_HEADLINE, build_headline

    abstain = _report_for(DoNothingPolicy(), dataset)
    bound = _report_for(OracleBestPolicy(dataset["outcomes_by_txn"]), dataset)

    assert build_headline([abstain, bound]) == NO_HEADLINE
    assert build_headline([abstain]) == NO_HEADLINE
    assert build_headline([bound]) == NO_HEADLINE
    assert build_headline([]) == NO_HEADLINE
    # And the refusal must not smuggle the bound's numbers in anyway.
    assert "CHEATS" not in build_headline([abstain, bound])


def test_headline_never_reports_a_subject_below_its_comparison(dataset: dict) -> None:
    """When a headline IS emitted, the subject must be the stronger policy."""
    from retry_economist.eval.cli import NO_HEADLINE, build_headline

    weak = _report_for(DoNothingPolicy(), dataset)
    strong = _report_for(_CapBustingPolicy(), dataset)
    # The cap-buster breaches compliance, so it is not eligible at all.
    assert build_headline([weak, strong]) == NO_HEADLINE

    # Make it compliant by fiat to exercise the comparison path itself.
    from dataclasses import replace

    from retry_economist.eval.simulator import RunResult

    compliant = replace(strong.result, violations=[])
    strong_ok = type(strong)(
        name="acts_everywhere",
        is_reference_bound=False,
        metrics=mx.compute_for_run(compliant),
        ci=strong.ci,
        result=compliant,
    )
    assert isinstance(compliant, RunResult)

    headline = build_headline([weak, strong_ok])
    assert headline != NO_HEADLINE
    assert headline.startswith("Recovered ")
    assert "acts_everywhere" not in headline, "the subject must not be its own comparison"
    assert weak.name in headline, "the baseline should be the comparison"
    # The subject's rate must lead, and it must be the higher of the two.
    subject_rate = float(headline.split("Recovered ")[1].split("%")[0])
    comparison_rate = float(headline.split(" vs ")[1].split("%")[0])
    assert subject_rate >= comparison_rate


# ---------------------------------------------------------------------------
# lifetime-value sensitivity
# ---------------------------------------------------------------------------


def test_clv_sweep_moves_costs_but_not_revenue(dataset: dict) -> None:
    """Net revenue is invariant to the guess; net value is not.

    If a conclusion moves with the lifetime-value assumption, the report has to
    say so - that is the whole reason the sweep exists.
    """
    from retry_economist.eval.cli import CLV_SWEEP_PAISE, run_clv_sweep, sweep_conclusion

    bound = _report_for(OracleBestPolicy(dataset["outcomes_by_txn"]), dataset)
    rows = run_clv_sweep([bound], CLV_SWEEP_PAISE)
    points = rows[0]["points"]

    assert len(points) == len(CLV_SWEEP_PAISE)
    assert len({p["net_rupees"] for p in points}) == 1, "net revenue must not move with CLV"
    # Annoyance cost is linear in CLV, so a 7.5x range must show a 7.5x spread.
    costs = [p["annoyance_cost_rupees"] for p in points]
    assert costs[0] < costs[1] < costs[2]
    assert costs[2] / costs[0] == pytest.approx(
        CLV_SWEEP_PAISE[2] / CLV_SWEEP_PAISE[0], rel=1e-3
    )
    # Net value falls as the relationship is priced higher.
    values = [p["net_value_rupees"] for p in points]
    assert values[0] > values[1] > values[2]
    assert rows[0]["verdict"]
    assert sweep_conclusion(rows)


def test_annoyance_is_priced_through_lifetime_value(dataset: dict) -> None:
    """The flat per-unit constant is gone; cost now scales with CLV and churn."""
    from retry_economist.eval import costs

    assert not hasattr(costs, "ANNOYANCE_PAISE_PER_UNIT"), "flat annoyance price must be gone"
    assert costs.annoyance_to_paise(1.0) == round(
        costs.ANNOYANCE_TO_CHURN_PER_UNIT * costs.CUSTOMER_LIFETIME_VALUE_PAISE
    )
    assert costs.annoyance_to_paise(2.0, clv_paise=400_000) == round(
        2.0 * costs.ANNOYANCE_TO_CHURN_PER_UNIT * 400_000
    )

    # And acting is now genuinely expensive: a contact-heavy policy must not
    # come out effectively free the way it did under the flat constant.
    m = mx.compute_for_run(_run(_CapBustingPolicy(), dataset, split="all"))
    assert m.annoyance_cost_rupees > m.total_cost_rupees, (
        "relationship damage should dominate per-action fees"
    )


# ---------------------------------------------------------------------------
# time to recovery
# ---------------------------------------------------------------------------


class _NeverRecoversPolicy:
    """Escalates only hard declines that no action can clear, so it never wins.

    Deliberately constructed to recover nothing: it is the only way to test that
    a policy with no recoveries reports None rather than a plausible-looking 0.
    """

    name = "never_recovers"

    def decide(self, txn: ObservedTransaction) -> Decision:
        return Decision(plan=[], reason="abstain")


def test_median_days_is_none_not_zero_when_nothing_recovers(dataset: dict) -> None:
    """A policy that recovers nothing did not recover instantly."""
    outcomes = [
        o
        for o in _run(_NeverRecoversPolicy(), dataset, split="all").outcomes
        if not o.recovered
    ]
    m = mx.compute(outcomes)

    assert m.recovery_rate == 0.0
    assert m.mean_days_to_recovery is None, "0.0 days would read as instant recovery"
    assert m.median_days_to_recovery is None
    assert m.recovered_within_72h_rate == 0.0

    # And the empty population behaves the same way rather than dividing by zero.
    empty = mx.compute([])
    assert empty.median_days_to_recovery is None
    assert empty.mean_days_to_recovery is None


def test_within_72h_rate_never_exceeds_recovery_rate(dataset: dict) -> None:
    """The fast subset cannot be larger than the whole.

    Both are shares of ALL transactions, so the gap between them is exactly the
    recovery a policy bought by waiting - which is the trade the timing columns
    exist to expose.
    """
    for policy in (DoNothingPolicy(), OracleBestPolicy(dataset["outcomes_by_txn"])):
        for split in ("all", "holdout"):
            m = mx.compute_for_run(_run(policy, dataset, split=split))
            assert m.recovered_within_72h_rate <= m.recovery_rate, (
                f"{policy.name}/{split}: {m.recovered_within_72h_rate} > {m.recovery_rate}"
            )
            for code, sub in m.per_failure_code.items():
                assert sub.recovered_within_72h_rate <= sub.recovery_rate, code


def test_timing_is_reported_per_failure_code(dataset: dict) -> None:
    m = mx.compute_for_run(_run(OracleBestPolicy(dataset["outcomes_by_txn"]), dataset))
    assert m.per_failure_code, "expected a per-failure-code breakdown"
    for code, sub in m.per_failure_code.items():
        if sub.recovery_rate > 0:
            assert sub.median_days_to_recovery is not None, code
            assert sub.mean_days_to_recovery is not None, code
            assert sub.median_days_to_recovery >= 0.0
        else:
            assert sub.median_days_to_recovery is None, code


# ---------------------------------------------------------------------------
# rupee weighting
# ---------------------------------------------------------------------------


def test_rupee_weighted_buckets_sum_to_the_total_at_risk(dataset: dict) -> None:
    """The seven buckets partition the money exactly, as well as the count."""
    policies = [
        DoNothingPolicy(),
        OracleBestPolicy(dataset["outcomes_by_txn"]),
        _CapBustingPolicy(),
    ]
    for policy in policies:
        result = _run(policy, dataset, split="all")
        m = mx.compute_for_run(result)
        buckets = (
            m.incremental,
            m.cannibalised,
            m.wasted,
            m.futile,
            m.correct_restraint,
            m.correct_walkaway,
            m.missed_opportunity,
        )
        # Exact in paise: money that does not add up exactly is money nobody
        # trusts, so this is an equality rather than an approximation.
        assert sum(b.paise for b in buckets) == round(m.total_rupees_at_risk * 100), policy.name
        assert sum(b.rupee_share for b in buckets) == pytest.approx(1.0)
        # And the total at risk is the whole population, however it is sliced.
        assert m.total_rupees_at_risk == pytest.approx(
            sum(o.amount_paise for o in result.outcomes) / 100.0
        )
        for code, sub in m.per_failure_code.items():
            sub_buckets = (
                sub.incremental,
                sub.cannibalised,
                sub.wasted,
                sub.futile,
                sub.correct_restraint,
                sub.correct_walkaway,
                sub.missed_opportunity,
            )
            assert sum(b.paise for b in sub_buckets) == round(sub.total_rupees_at_risk * 100), code


def test_rupee_and_count_views_are_reported_together(dataset: dict) -> None:
    """Both weightings must survive into the artefacts, neither replacing the other."""
    m = mx.compute_for_run(_run(OracleBestPolicy(dataset["outcomes_by_txn"]), dataset))
    payload = m.to_dict()

    assert payload["recovery_rate"] == round(m.recovery_rate, 6)
    assert payload["rupee_weighted"]["recovery_rate"] == round(m.rupee_recovery_rate, 6)
    assert payload["rupee_weighted"]["net_uplift_pp"] == round(m.rupee_net_uplift_pp, 4)
    assert payload["timing"]["recovered_within_72h_rate"] == round(m.recovered_within_72h_rate, 6)
    for bucket in payload["acted_buckets"].values():
        assert "share" in bucket and "rupee_share" in bucket
    # The per-failure-code rows carry both views too.
    for sub in payload["per_failure_code"].values():
        assert "rupee_weighted" in sub and "timing" in sub


def test_abstaining_carries_no_rupee_uplift(dataset: dict) -> None:
    """Uplift is zero under both weightings when nothing is ever done."""
    m = mx.compute_for_run(_run(DoNothingPolicy(), dataset, split="all"))
    assert m.net_uplift_pp == 0.0
    assert m.rupee_net_uplift_pp == pytest.approx(0.0)
    assert m.rupee_recovery_rate == pytest.approx(m.rupee_organic_rate)


def test_empty_and_zero_recovery_slices_render_differently(dataset: dict) -> None:
    """Three absences, three renderings - and the distinction survives JSON.

    "no transactions here" and "transactions, none recovered" are different
    facts about a policy, and both used to print as a bare "n/a".
    """
    from retry_economist.eval.cli import _days

    empty = mx.compute([])
    assert _days(empty.median_days_to_recovery, empty) == "-"
    assert empty.to_dict()["timing"]["n"] == 0
    assert empty.to_dict()["timing"]["n_recovered"] == 0

    nothing_recovered = mx.compute(
        [o for o in _run(DoNothingPolicy(), dataset, split="all").outcomes if not o.recovered]
    )
    assert nothing_recovered.n > 0
    assert _days(nothing_recovered.median_days_to_recovery, nothing_recovered) == "n/a (0 recovered)"
    assert nothing_recovered.to_dict()["timing"]["n_recovered"] == 0
    assert nothing_recovered.to_dict()["timing"]["n"] == nothing_recovered.n

    real = mx.compute_for_run(_run(DoNothingPolicy(), dataset, split="all"))
    assert _days(real.median_days_to_recovery, real) not in {"-", "n/a (0 recovered)"}
    assert real.to_dict()["timing"]["n_recovered"] == real.n_recovered > 0
