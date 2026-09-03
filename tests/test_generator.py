"""Contract tests for the synthetic data generator.

These are not unit tests of arithmetic; they are guards on the four properties
that make the dataset usable as a benchmark at all:

1. it is reproducible from a seed,
2. the agent-visible feed leaks no latent state,
3. the splits do not leak a customer across the train/holdout boundary,
4. the causal structure has real headroom - an informed policy beats a blind
   one, and hard declines stay unrecoverable no matter how often you retry.

If any of these break, every downstream number is meaningless, so they are
asserted directly rather than inferred from spot checks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from retry_economist.generator.cli import generate, split_for  # noqa: E402
from retry_economist.schema import ACTIONS, RETRY_ACTIONS  # noqa: E402

SEED = 42

#: Both the small development scale and the canonical dataset scale. Every
#: threshold below is asserted at both, so a number that only holds at one size
#: is a number that was fitted rather than measured.
SCALES = ((800, 300), (2500, 900))

#: Spelled out here on purpose. Importing the ban list from the code under test
#: would let a rename quietly disable this check.
FORBIDDEN_KEYS = (
    "intent_to_pay",
    "liquidity",
    "salary_day",
    "annoyance",
    "hard_blocked",
    "would_pay_anyway",
)

DATASET_FILES = ("observed.jsonl", "oracle.jsonl", "splits.json", "summary.md")


def _read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


@pytest.fixture(scope="module", params=SCALES, ids=lambda s: f"n{s[0]}_c{s[1]}")
def dataset(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> dict:
    """One generated dataset per scale, shared by every test in the module."""
    n, n_customers = request.param
    out = tmp_path_factory.mktemp(f"generated_{n}")
    generate(seed=SEED, n=n, n_customers=n_customers, out_dir=out)
    return {
        "n": n,
        "n_customers": n_customers,
        "dir": out,
        "observed": _read_jsonl(out / "observed.jsonl"),
        "oracle": _read_jsonl(out / "oracle.jsonl"),
        "splits": json.loads((out / "splits.json").read_text(encoding="utf-8")),
    }


@pytest.mark.parametrize(("n", "n_customers"), SCALES)
def test_cli_is_byte_reproducible(tmp_path: Path, n: int, n_customers: int) -> None:
    """Two CLI runs at the same seed must produce identical bytes.

    Run as a subprocess rather than in-process so this also covers module import
    order and any accidental use of the global `random` module.
    """
    env_path = str(SRC)
    runs = []
    for name in ("run_a", "run_b"):
        out = tmp_path / f"{name}_{n}"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "retry_economist.generator.cli",
                "--seed",
                str(SEED),
                "--n",
                str(n),
                "--customers",
                str(n_customers),
                "--out",
                str(out),
            ],
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONPATH": env_path},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        runs.append(out)

    for filename in DATASET_FILES:
        a = (runs[0] / filename).read_bytes()
        b = (runs[1] / filename).read_bytes()
        assert a and a == b, f"{filename} differs between identical-seed runs"


def test_observed_feed_contains_no_latent_fields(dataset: dict) -> None:
    for row in dataset["observed"]:
        for key in FORBIDDEN_KEYS:
            assert key not in row, f"latent field {key!r} leaked into observed.jsonl"
        # A nested container would be an easy way to smuggle latent state past
        # a flat key check.
        assert all(not isinstance(v, (dict, list)) for v in row.values())


def test_every_observed_txn_appears_exactly_once_in_oracle(dataset: dict) -> None:
    n = dataset["n"]
    observed_ids = [row["txn_id"] for row in dataset["observed"]]
    oracle_ids = Counter(row["txn_id"] for row in dataset["oracle"])

    assert len(observed_ids) == n
    assert len(set(observed_ids)) == n, "duplicate txn_id in observed.jsonl"
    for txn_id in observed_ids:
        assert oracle_ids[txn_id] == 1, f"{txn_id} appears {oracle_ids[txn_id]}x in oracle.jsonl"
    assert sum(oracle_ids.values()) == n, "oracle.jsonl has rows with no observed twin"


def test_oracle_records_every_action(dataset: dict) -> None:
    for row in dataset["oracle"]:
        assert set(row["outcomes"]) == set(ACTIONS)
        assert row["would_pay_anyway"] == row["outcomes"]["do_nothing"]["recovered"]


def test_organic_recovery_is_plausible(dataset: dict) -> None:
    """`do_nothing` must recover a meaningful but minority share.

    Too low and 'would have paid anyway' stops being a real confounder; too high
    and the dataset says intervention is pointless. Published dunning benchmarks
    sit around a fifth to a quarter.
    """
    rate = sum(r["would_pay_anyway"] for r in dataset["oracle"]) / len(dataset["oracle"])
    assert 0.15 < rate < 0.32, f"organic recovery rate {rate:.3f} outside plausible band"


def test_oracle_policy_beats_blind_retry(dataset: dict) -> None:
    """There must be real headroom for an intelligent router to win."""
    oracle = dataset["oracle"]
    n = len(oracle)
    best = sum(any(r["outcomes"][a]["recovered"] for a in ACTIONS) for r in oracle) / n
    blind = sum(r["outcomes"]["retry_now"]["recovered"] for r in oracle) / n
    organic = sum(r["would_pay_anyway"] for r in oracle) / n

    assert best > blind + 0.15, f"oracle {best:.3f} barely beats retry_now {blind:.3f}"
    assert blind > organic, "blind retry should still beat doing nothing on average"


def test_splits_never_share_a_customer(dataset: dict) -> None:
    splits = dataset["splits"]
    train, holdout = set(splits["train"]), set(splits["holdout"])

    assert train and holdout
    assert not (train & holdout), "customer present in both train and holdout"
    # And the assignment is a pure function of the id, so a transaction-level
    # split can never sneak back in.
    for row in dataset["observed"]:
        cid = row["customer_id"]
        assert (cid in train) == (split_for(cid) == "train")


def test_hard_declines_are_never_recovered_by_retrying(dataset: dict) -> None:
    """A blocked card does not unblock because you asked it again.

    Only `request_new_mandate` and `escalate_to_human` may ever recover these,
    and both must actually manage it sometimes or the action space is dead
    weight for this whole segment.
    """
    hard = [r for r in dataset["oracle"] if r["latent"]["decline_type"] == "hard"]
    assert hard, "dataset generated no hard declines"

    for row in hard:
        for action in RETRY_ACTIONS:
            assert not row["outcomes"][action]["recovered"], (
                f"{row['txn_id']} ({row['failure_mode']}) recovered via {action}"
            )
        for action in ("do_nothing", "nudge_then_retry", "switch_to_upi_intent"):
            assert not row["outcomes"][action]["recovered"], (
                f"{row['txn_id']} ({row['failure_mode']}) recovered via {action}"
            )

    rescued = sum(
        row["outcomes"]["request_new_mandate"]["recovered"]
        or row["outcomes"]["escalate_to_human"]["recovered"]
        for row in hard
    )
    assert 0 < rescued < len(hard), "hard declines must be sometimes-recoverable, not never/always"
