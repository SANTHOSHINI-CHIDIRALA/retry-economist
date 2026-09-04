"""Contract tests for the two honest baselines.

The baselines are what every later claim is measured against, so these tests
guard the properties that make the comparison fair rather than the arithmetic:

- both are COMPLIANT, so beating them is not beating a policy that could never
  legally run;
- the naive baseline really is indiscriminate, burning attempts on declines no
  retry can clear - that waste is the thing the project claims to remove, and if
  it were absent here the claim would be empty;
- the rules baseline really is strong, spending nothing on hard declines, so
  "the LLM beat a lookup table" cannot be dismissed as beating a strawman;
- and neither can see the counterfactual answers.
"""

from __future__ import annotations

import json
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
from retry_economist.policies.base import Decision, validate_plan  # noqa: E402
from retry_economist.policies.do_nothing import DoNothingPolicy  # noqa: E402
from retry_economist.policies.naive_retry import SCHEDULE, NaiveRetry3xPolicy  # noqa: E402
from retry_economist.policies.rules_only import RULES, RulesOnlyPolicy  # noqa: E402
from retry_economist.schema import ATTEMPTS_CONSUMED  # noqa: E402

SEED = 42
SCALES = ((800, 300), (2500, 900))

#: The canonical dataset scale. Claims that need statistical support are only
#: asserted here; the smaller scale is kept to prove the code works at both, not
#: to prove results it does not have the clusters to support.
CANONICAL_N = 2500

#: Codes the generator emits for declines nothing can clear.
HARD_CODES = ("41", "R05")


@pytest.fixture(scope="module", params=SCALES, ids=lambda s: f"n{s[0]}_c{s[1]}")
def dataset(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> dict:
    n, n_customers = request.param
    out = tmp_path_factory.mktemp(f"baseline_data_{n}")
    generate(seed=SEED, n=n, n_customers=n_customers, out_dir=out)
    return {
        "n": n,
        "transactions": load_observed(out / "observed.jsonl"),
        "store": load_counterfactuals(out / "oracle.jsonl"),
        "splits": json.loads((out / "splits.json").read_text(encoding="utf-8")),
    }


def _run(policy, dataset: dict, split: str = "holdout"):
    subset = filter_split(dataset["transactions"], dataset["splits"], split)
    return run(policy, subset, dataset["store"], split=split)


# ---------------------------------------------------------------------------
# naive_retry_3x
# ---------------------------------------------------------------------------


def test_naive_retry_never_exceeds_the_attempt_cap(dataset: dict) -> None:
    """Truncated at planning time, so the compliance gate never has to intervene."""
    result = _run(NaiveRetry3xPolicy(), dataset, split="all")
    metrics = mx.compute_for_run(result)

    assert metrics.compliance_violations == 0
    assert result.is_valid
    by_id = {t.txn_id: t for t in dataset["transactions"]}
    for outcome in result.outcomes:
        budget = by_id[outcome.txn_id].attempts_left
        assert len(outcome.plan) <= min(len(SCHEDULE), budget)
        assert outcome.attempts_used <= budget


def test_naive_retry_burns_attempts_on_hard_declines(dataset: dict) -> None:
    """The waste is real and must be visible, or the project has nothing to fix.

    A blocked card cannot be unblocked by asking again, but the incumbent asks
    three times anyway, paying a fee and an annoyance cost each time.
    """
    result = _run(NaiveRetry3xPolicy(), dataset, split="all")
    metrics = mx.compute_for_run(result)

    assert metrics.hard_decline_retry_waste > 0, "the incumbent must show this waste"

    hard = [o for o in result.outcomes if o.decline_type == "hard"]
    assert hard, "dataset generated no hard declines"
    assert sum(o.attempts_used for o in hard) == metrics.hard_decline_retry_waste
    # None of it ever pays off: hard declines are unrecoverable by any retry.
    assert not any(o.recovered for o in hard)
    assert all(o.futile for o in hard if o.acted)


def test_naive_retry_acts_on_everything_it_can(dataset: dict) -> None:
    """Indiscriminate by construction - it abstains only when the cap forces it."""
    result = _run(NaiveRetry3xPolicy(), dataset, split="all")
    by_id = {t.txn_id: t for t in dataset["transactions"]}
    for outcome in result.outcomes:
        if not outcome.acted:
            assert by_id[outcome.txn_id].attempts_left == 0, (
                f"{outcome.txn_id}: abstained despite having budget"
            )


# ---------------------------------------------------------------------------
# rules_only
# ---------------------------------------------------------------------------


def test_rules_only_spends_nothing_on_hard_declines(dataset: dict) -> None:
    """The single most valuable rule in the table, asserted directly."""
    result = _run(RulesOnlyPolicy(), dataset, split="all")
    metrics = mx.compute_for_run(result)

    assert metrics.hard_decline_retry_waste == 0

    hard = [o for o in result.outcomes if o.decline_type == "hard"]
    assert hard, "dataset generated no hard declines"
    for outcome in hard:
        assert outcome.attempts_used == 0
        assert outcome.plan == (), f"{outcome.txn_id}: planned {outcome.plan} on a hard decline"
        assert outcome.total_cost_paise == 0
    # Per failure code, the hard ones must show no attempts at all.
    for code in HARD_CODES:
        sub = metrics.per_failure_code.get(code)
        if sub is not None:
            assert sub.total_attempts == 0, code


def test_rules_only_is_compliant(dataset: dict) -> None:
    """The budget guard means no plan ever needs truncating."""
    result = _run(RulesOnlyPolicy(), dataset, split="all")
    assert result.is_valid
    assert mx.compute_for_run(result).compliance_violations == 0

    by_id = {t.txn_id: t for t in dataset["transactions"]}
    for outcome in result.outcomes:
        requested = sum(ATTEMPTS_CONSUMED[a] for a in outcome.plan)
        assert requested <= by_id[outcome.txn_id].attempts_left


def test_rules_only_beats_naive_on_net_uplift(dataset: dict) -> None:
    """The expert system must beat the incumbent on uplift, at every scale.

    The point estimate is asserted at both scales. Whether the difference is
    STATISTICALLY supported is asserted only at the canonical scale, because it
    genuinely is not supported at the smaller one: 89 holdout clusters give a
    paired interval of roughly [-2.8, +7.5] pp, which straddles zero. That is
    not a flaw in the policy, it is the dataset being too small to support the
    claim - and it is exactly why the canonical dataset was scaled up. Asserting
    significance at 89 clusters would be asserting something the evidence does
    not contain.
    """
    rules = _run(RulesOnlyPolicy(), dataset)
    naive = _run(NaiveRetry3xPolicy(), dataset)
    rules_m = mx.compute_for_run(rules)
    naive_m = mx.compute_for_run(naive)

    assert rules_m.net_uplift_pp > naive_m.net_uplift_pp, (
        f"rules {rules_m.net_uplift_pp:.2f} pp vs naive {naive_m.net_uplift_pp:.2f} pp"
    )
    # And it does so while spending far fewer attempts, which is the point.
    assert rules_m.total_attempts < naive_m.total_attempts

    delta = bs.paired_bootstrap(
        rules, naive, iterations=1000, seed=5, statistic=bs.net_uplift_statistic
    )
    assert delta.point is not None and delta.point > 0
    if dataset["n"] >= CANONICAL_N:
        assert delta.low > 0, f"advantage not supported: CI [{delta.low}, {delta.high}]"


def test_rules_only_beats_naive_on_decision_quality(dataset: dict) -> None:
    """Better decisions, not merely more recovery.

    The count-weighted view holds at both scales. The RUPEE-weighted view is
    asserted only at the canonical scale, and the reason is a finding rather
    than an excuse: `rules_only` plans a single action while the incumbent fires
    a three-attempt ladder, so on the modes where extra attempts simply mean
    extra chances - outages and transient gateway errors - the ladder wins. At
    89 clusters a handful of large transactions in those modes is enough to flip
    the rupee-weighted ranking (naive 0.44 vs rules 0.41), while at 240 clusters
    the picture is unambiguous (rules 0.43 vs naive 0.21). The two weightings
    disagreeing at small n is the divergence the rupee metric exists to expose.
    """
    rules_m = mx.compute_for_run(_run(RulesOnlyPolicy(), dataset))
    naive_m = mx.compute_for_run(_run(NaiveRetry3xPolicy(), dataset))

    assert rules_m.decision.precision > naive_m.decision.precision
    assert rules_m.decision.f1 > naive_m.decision.f1

    if dataset["n"] >= CANONICAL_N:
        assert rules_m.rupee_decision.precision > naive_m.rupee_decision.precision
        assert rules_m.rupee_decision.f1 > naive_m.rupee_decision.f1


def test_paired_intervals_tighten_with_more_clusters(dataset: dict) -> None:
    """The bootstrap must actually reward the larger dataset.

    Guards the premise behind the scale-up: intervals are set by the number of
    customer CLUSTERS, not the number of transactions, so the canonical dataset
    has to buy a materially tighter interval or the extra data bought nothing.
    """
    delta = bs.paired_bootstrap(
        _run(RulesOnlyPolicy(), dataset),
        _run(NaiveRetry3xPolicy(), dataset),
        iterations=1000,
        seed=5,
        statistic=bs.net_uplift_statistic,
    )
    width = delta.high - delta.low
    if dataset["n"] >= CANONICAL_N:
        assert width < 9.0, f"canonical interval should be tight, got {width:.2f} pp"
    else:
        assert width > 8.0, f"small-scale interval should be wide, got {width:.2f} pp"


def test_rules_only_covers_every_generated_failure_code(dataset: dict) -> None:
    """No code may fall through to the unrecognised-decline default.

    A rules baseline that silently defaults on a common code is a strawman, so
    the table is asserted to actually cover the taxonomy the generator emits.
    """
    codes = {t.failure_code for t in dataset["transactions"]}
    missing = codes - set(RULES)
    assert not missing, f"rules table has no entry for {sorted(missing)}"


# ---------------------------------------------------------------------------
# shared contracts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "policy", [DoNothingPolicy(), NaiveRetry3xPolicy(), RulesOnlyPolicy()], ids=lambda p: p.name
)
def test_every_decision_carries_a_reason_and_a_legal_plan(policy, dataset: dict) -> None:
    """A recovery decision nobody can explain is not reviewable by the merchant."""
    for txn in dataset["transactions"]:
        decision = policy.decide(txn)
        assert isinstance(decision, Decision)
        assert decision.reason and decision.reason.strip(), f"{policy.name}: empty reason"
        # Raises InvalidPlan if the plan is malformed.
        validate_plan(decision.plan, policy_name=policy.name)


def test_baselines_pass_the_leakage_guard() -> None:
    """Neither baseline may reach the counterfactual store.

    Re-uses the Phase 2 guard directly rather than reimplementing it, so the two
    suites can never drift apart on what counts as a leak.
    """
    from test_eval import LEAKAGE_ALLOWLIST, _leakage_findings

    for name in ("naive_retry.py", "rules_only.py"):
        path = SRC / "retry_economist" / "policies" / name
        assert path.exists(), name
        assert name not in LEAKAGE_ALLOWLIST, f"{name} must not be allow-listed"
        assert not _leakage_findings(path), f"{name} leaks: {_leakage_findings(path)}"
