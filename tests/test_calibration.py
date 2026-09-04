"""Tests for the calibration measurement, and for the one way it could lie.

The contaminated-baseline failure is the dangerous one. If the per-failure-code
prior were fitted on the split it is scored against, it would have seen the very
outcomes it is predicting, making it artificially strong - and a router that
lost to it would look worse than it is, while a router that beat it would have
beaten something impossible. So the fit is asserted to touch train only.
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

from retry_economist.eval import calibration as cal  # noqa: E402
from retry_economist.eval.simulator import (  # noqa: E402
    filter_split,
    load_counterfactuals,
    load_observed,
)
from retry_economist.generator.cli import generate  # noqa: E402
from retry_economist.llm.provider import CachingProvider, MockProvider  # noqa: E402
from retry_economist.llm.cache import ResponseCache  # noqa: E402
from retry_economist.policies.llm_router_only import LLMRouterOnlyPolicy  # noqa: E402
from retry_economist.router.router import Router  # noqa: E402
from retry_economist.router.signals import SignalIndex  # noqa: E402


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> dict:
    out = tmp_path_factory.mktemp("calibration_data")
    generate(seed=42, n=800, n_customers=300, out_dir=out)
    transactions = load_observed(out / "observed.jsonl")
    store = load_counterfactuals(out / "oracle.jsonl")
    splits = json.loads((out / "splits.json").read_text(encoding="utf-8"))
    return {
        "transactions": transactions,
        "store": store,
        "splits": splits,
        "train": filter_split(transactions, splits, "train"),
        "holdout": filter_split(transactions, splits, "holdout"),
        "cache_dir": out / "cache",
    }


@pytest.fixture(scope="module")
def routed(dataset: dict) -> dict:
    holdout = dataset["holdout"]
    provider = CachingProvider(MockProvider(), ResponseCache(dataset["cache_dir"]))
    policy = LLMRouterOnlyPolicy(Router(provider, SignalIndex(holdout)))
    for txn in holdout:
        policy.decide(txn)
    prior = cal.HistoricalPrior.fit(dataset["train"], dataset["store"])
    records = cal.build_records(policy.proposals, holdout, dataset["store"])
    return {"prior": prior, "records": records, "report": cal.evaluate(records, prior)}


# ---------------------------------------------------------------------------
# the contamination guard
# ---------------------------------------------------------------------------


def test_historical_prior_is_fitted_on_train_only(dataset: dict, routed: dict) -> None:
    """No holdout customer may contribute to the baseline being compared against.

    A prior fitted on the scored split has already seen those outcomes. It would
    be an oracle wearing a baseline's clothes, and every comparison against it
    would be meaningless.
    """
    prior = routed["prior"]
    holdout_customers = {t.customer_id for t in dataset["holdout"]}
    train_customers = {t.customer_id for t in dataset["train"]}

    assert prior.fitted_on_customers <= train_customers
    leaked = prior.fitted_on_customers & holdout_customers
    assert not leaked, f"prior was fitted on {len(leaked)} holdout customers"
    assert prior.n_fitted == len(dataset["train"])
    # And the splits really are disjoint, so the guard above means something.
    assert not (train_customers & holdout_customers)


def test_prior_covers_the_codes_it_is_asked_about(dataset: dict, routed: dict) -> None:
    """A prior that falls through to a global mean everywhere is a weak baseline."""
    prior = routed["prior"]
    codes = {r.failure_code for r in routed["records"]}
    covered = codes & set(prior.abstain_by_code)
    assert len(covered) == len(codes), f"prior missing codes: {codes - covered}"


# ---------------------------------------------------------------------------
# scoring mechanics
# ---------------------------------------------------------------------------


def test_brier_is_the_mean_squared_error() -> None:
    assert cal.brier([1.0, 0.0], [True, False]) == 0.0
    assert cal.brier([0.0, 1.0], [True, False]) == 1.0
    assert cal.brier([0.5, 0.5], [True, False]) == 0.25
    assert cal.brier([], []) is None


def test_a_perfect_forecaster_scores_zero_and_a_useless_one_scores_worse() -> None:
    outcomes = [True, True, False, False, True]
    perfect = [1.0, 1.0, 0.0, 0.0, 1.0]
    hedged = [0.5] * 5
    assert cal.brier(perfect, outcomes) < cal.brier(hedged, outcomes)


def test_reliability_bins_partition_the_predictions() -> None:
    predictions = [0.05, 0.15, 0.15, 0.95]
    outcomes = [False, True, False, True]
    table = cal.reliability_table(predictions, outcomes)

    assert len(table) == cal.N_BINS
    assert sum(b.count for b in table) == len(predictions)
    second = table[1]
    assert second.count == 2
    assert second.mean_predicted == pytest.approx(0.15)
    assert second.observed_frequency == pytest.approx(0.5)
    # An empty bin reports nothing rather than a misleading zero.
    assert table[5].count == 0 and table[5].mean_predicted is None


def test_plan_recovery_ground_truth_follows_the_simulator(dataset: dict) -> None:
    """The act-side outcome must be the same walk the simulator performs."""
    store = dataset["store"]
    txn_id = dataset["holdout"][0].txn_id
    outcomes = store[txn_id]["outcomes"]
    winning = next((a for a, o in outcomes.items() if o["recovered"] and a != "do_nothing"), None)

    assert cal.plan_recovers([], outcomes) is False
    if winning:
        assert cal.plan_recovers([winning], outcomes) is True
        assert cal.plan_recovers(["retry_now", winning], outcomes) is True


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------


def test_act_estimates_are_only_scored_where_the_router_proposed_acting(routed: dict) -> None:
    """Grading a forecast nobody made would be scoring noise."""
    records, report = routed["records"], routed["report"]
    acted = [r for r in records if r.proposed_plan]

    assert report.n_act_scored == len(acted)
    assert report.n_abstain_scored == len(records)
    assert report.n_act_scored < report.n_records, "expected some abstain proposals"
    for record in records:
        assert (record.act_outcome is None) == (not record.proposed_plan)


def test_report_states_a_verdict_in_whichever_direction_it_falls(routed: dict) -> None:
    report = routed["report"]
    for score in (report.act, report.abstain):
        assert score.router_brier is not None
        assert score.historical_brier is not None
        assert score.beats_historical in (True, False)
        verdict = score.verdict()
        assert ("BEATS" in verdict) or ("does NOT beat" in verdict)
    json.dumps(report.to_dict())


def test_all_three_predictors_are_scored_on_identical_outcomes(routed: dict) -> None:
    """Otherwise the comparison is between different questions."""
    report = routed["report"]
    for score in (report.act, report.abstain):
        assert score.n > 0
        for value in (score.router_brier, score.constant_brier, score.historical_brier):
            assert value is not None and 0.0 <= value <= 1.0
    # The constant predictor cannot beat a per-code one that nests it, except by
    # noise; assert the weaker claim that both are in a sane range.
    assert report.abstain.constant_brier <= 0.35


def test_calibration_is_reproducible(dataset: dict) -> None:
    """Same inputs, same numbers - the whole point of a deterministic stand-in."""

    def score() -> dict:
        holdout = dataset["holdout"]
        provider = CachingProvider(MockProvider(), ResponseCache(dataset["cache_dir"]))
        policy = LLMRouterOnlyPolicy(Router(provider, SignalIndex(holdout)))
        for txn in holdout:
            policy.decide(txn)
        prior = cal.HistoricalPrior.fit(dataset["train"], dataset["store"])
        return cal.evaluate(
            cal.build_records(policy.proposals, holdout, dataset["store"]), prior
        ).to_dict()

    assert score() == score()
