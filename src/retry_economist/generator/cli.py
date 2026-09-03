"""Entry point: build a world, fail 800 payments in it, and write the dataset.

Install the package once (`pip install -e .`), then run from anywhere:

    python -m retry_economist.generator.cli --seed 42 --n 800

Files are written with explicit LF newlines and no timestamps anywhere, so two
runs at the same seed are byte-identical on any platform. That property is not
cosmetic: it is what lets a reviewer re-derive the exact dataset an experiment
was run against from the seed alone.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import timedelta
from pathlib import Path
from random import Random
from statistics import mean
from typing import Any, Iterable

from retry_economist.generator.customers import Customer, build_customers, customer_public_fields
from retry_economist.generator.failures import generate_transactions
from retry_economist.generator.outcomes import build_oracle
from retry_economist.generator.world import build_world, world_report
from retry_economist.schema import (
    ACTIONS,
    FORBIDDEN_OBSERVED_FIELDS,
    OBSERVED_FIELDS,
    OracleRecord,
    SIM_END,
    SIM_START,
    Transaction,
    transaction_public_fields,
)

DEFAULT_OUT = Path("data/generated")
TRAIN_FRACTION = 70  # out of 100 hash buckets


def split_for(customer_id: str) -> str:
    """Assign a customer to train or holdout by hashing their id.

    Splitting by customer rather than by transaction is the whole point: the
    same payer's failures are highly correlated, so a transaction-level split
    would leak their latent state across the boundary and flatter any model
    that memorises customers instead of learning the causal structure.
    """
    bucket = int.from_bytes(hashlib.sha256(customer_id.encode("utf-8")).digest()[:4], "big") % 100
    return "train" if bucket < TRAIN_FRACTION else "holdout"


def observed_record(txn: Transaction, customer: Customer) -> dict[str, Any]:
    """The agent-visible view of one failure, in the declared field order."""
    merged = {**transaction_public_fields(txn), **customer_public_fields(customer)}
    record = {key: merged[key] for key in OBSERVED_FIELDS}
    leaked = set(record) - set(OBSERVED_FIELDS)
    if leaked:  # pragma: no cover - guarded by construction and by the tests
        raise AssertionError(f"latent leak into observed feed: {sorted(leaked)}")
    return record


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_text(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _oracle_best_action(record: OracleRecord) -> str | None:
    """Cheapest action that recovers this payment, or None if nothing does.

    ACTIONS is ordered from least to most invasive, so scanning it in order and
    taking the first success is a real policy - it prefers silence over a retry
    and a retry over a phone call whenever both would have worked.
    """
    for action in ACTIONS:
        if record.outcomes[action].recovered:
            return action
    return None


def _percentile(sorted_values: list[int], q: float) -> int:
    if not sorted_values:
        return 0
    idx = min(len(sorted_values) - 1, int(q * len(sorted_values)))
    return sorted_values[idx]


def _summary(
    txns: list[Transaction],
    oracle: list[OracleRecord],
    customers: dict[str, Customer],
    splits: dict[str, list[str]],
    world_rows: list[dict[str, object]],
    seed: int,
) -> str:
    n = len(txns)
    rupees = sorted(t.amount_paise for t in txns)
    by_action = {a: sum(r.outcomes[a].recovered for r in oracle) for a in ACTIONS}
    best = [_oracle_best_action(r) for r in oracle]
    recoverable = sum(b is not None for b in best)
    organic = by_action["do_nothing"]
    retry_now = by_action["retry_now"]

    def pct(count: int) -> str:
        return f"{count / n:6.1%}"

    lines: list[str] = [
        "# Retry Economist - synthetic dataset summary",
        "",
        f"- seed: `{seed}`",
        f"- failed transactions: **{n}**",
        f"- distinct customers in the feed: **{len({t.customer_id for t in txns})}** "
        f"of {len(customers)} simulated",
        # SIM_END is exclusive, so the last simulated day is the day before it.
        f"- calendar: {SIM_START.date()} to {(SIM_END - timedelta(days=1)).date()} (IST, "
        f"{(SIM_END - SIM_START).days} days)",
        "",
        "## Headline numbers",
        "",
        "| metric | value | why it matters |",
        "| --- | --- | --- |",
        f"| organic recovery (`do_nothing`) | {pct(organic)} | ground-truth "
        "*would have paid anyway*; any evaluation that ignores it overstates every policy |",
        f"| flat `retry_now` for everything | {pct(retry_now)} | the naive baseline to beat |",
        f"| oracle-best policy | {pct(recoverable)} | ceiling with perfect knowledge |",
        f"| headroom over naive retry | {(recoverable - retry_now) / n:6.1%} | "
        "the size of the prize for routing intelligently |",
        f"| uplift of oracle policy over silence | {(recoverable - organic) / n:6.1%} | "
        "recoveries that genuinely required an action |",
        "",
        "## Recovery rate by action (each applied to every transaction)",
        "",
        "| action | recovered | mean annoyance delta | mean hours to recovery |",
        "| --- | --- | --- | --- |",
    ]

    for action in ACTIONS:
        outs = [r.outcomes[action] for r in oracle]
        hrs = [o.hours_to_recovery for o in outs if o.hours_to_recovery is not None]
        lines.append(
            f"| `{action}` | {pct(by_action[action])} | "
            f"{mean(o.customer_annoyance_delta for o in outs):.3f} | "
            f"{(mean(hrs) if hrs else 0.0):.1f} |"
        )

    lines += ["", "## Oracle policy: which action it reaches for", ""]
    lines.append("| action | share of transactions |")
    lines.append("| --- | --- |")
    chosen = Counter(b or "unrecoverable" for b in best)
    for action in (*ACTIONS, "unrecoverable"):
        if chosen[action]:
            lines.append(f"| `{action}` | {pct(chosen[action])} |")

    lines += ["", "## Failure modes", "", "| mode | count | share | organic | oracle-best |", "| --- | --- | --- | --- | --- |"]
    mode_counts = Counter(t.failure_mode for t in txns)
    for mode, count in mode_counts.most_common():
        rows = [r for r in oracle if r.failure_mode == mode]
        org = sum(r.would_pay_anyway for r in rows) / count
        bst = sum(_oracle_best_action(r) is not None for r in rows) / count
        lines.append(f"| `{mode}` | {count} | {count / n:5.1%} | {org:5.1%} | {bst:5.1%} |")

    lines += ["", "## Method mix", "", "| method | count | share |", "| --- | --- | --- |"]
    for method, count in Counter(t.method for t in txns).most_common():
        lines.append(f"| `{method}` | {count} | {count / n:5.1%} |")

    lines += [
        "",
        "## Issuer mix and health",
        "",
        "| issuer | failures | share | class | base success | outage windows | outage hours |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    issuer_counts = Counter(t.issuer for t in txns)
    for row in world_rows:
        code = str(row["issuer"])
        count = issuer_counts[code]
        lines.append(
            f"| `{code}` | {count} | {count / n:5.1%} | {row['reliability_class']} | "
            f"{row['base_success_rate']} | {row['downtime_windows']} | {row['downtime_hours']} |"
        )

    lines += [
        "",
        "## Amounts",
        "",
        "| statistic | value (INR) |",
        "| --- | --- |",
        f"| min | {rupees[0] / 100:,.2f} |",
        f"| p25 | {_percentile(rupees, 0.25) / 100:,.2f} |",
        f"| median | {_percentile(rupees, 0.50) / 100:,.2f} |",
        f"| p75 | {_percentile(rupees, 0.75) / 100:,.2f} |",
        f"| p95 | {_percentile(rupees, 0.95) / 100:,.2f} |",
        f"| max | {rupees[-1] / 100:,.2f} |",
        f"| mean | {mean(rupees) / 100:,.2f} |",
        f"| total at risk | {sum(rupees) / 100:,.2f} |",
        "",
        "## Splits (by hash of customer_id, never by transaction)",
        "",
        "| split | customers | transactions |",
        "| --- | --- | --- |",
    ]
    for name in ("train", "holdout"):
        members = set(splits[name])
        lines.append(
            f"| {name} | {len(members)} | {sum(t.customer_id in members for t in txns)} |"
        )

    lines.append("")
    return "\n".join(lines)


def generate(seed: int, n: int, n_customers: int, out_dir: Path) -> dict[str, Path]:
    """Build the full dataset and write it to `out_dir`.

    A single RNG threads through world -> customers -> transactions so that the
    seed alone reproduces the run; the oracle then re-seeds per transaction from
    it, which keeps counterfactuals independent of loop order.
    """
    rng = Random(seed)
    world = build_world(rng)
    customers = build_customers(rng, n_customers)
    txns = generate_transactions(rng, world, customers, n)
    oracle = build_oracle(txns, customers, world, seed)

    splits: dict[str, list[str]] = {"train": [], "holdout": []}
    for cid in sorted(customers):
        splits[split_for(cid)].append(cid)

    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "observed": out_dir / "observed.jsonl",
        "oracle": out_dir / "oracle.jsonl",
        "splits": out_dir / "splits.json",
        "summary": out_dir / "summary.md",
    }

    _write_jsonl(paths["observed"], (observed_record(t, customers[t.customer_id]) for t in txns))
    _write_jsonl(paths["oracle"], (r.to_dict() for r in oracle))
    _write_text(
        paths["splits"],
        json.dumps(
            {
                "seed": seed,
                "strategy": "sha256(customer_id) % 100 < 70",
                "train_fraction": TRAIN_FRACTION / 100,
                "counts": {k: len(v) for k, v in splits.items()},
                "train": splits["train"],
                "holdout": splits["holdout"],
            },
            indent=2,
        )
        + "\n",
    )
    _write_text(
        paths["summary"],
        _summary(txns, oracle, customers, splits, world_report(world), seed),
    )
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="retry_economist.generator.cli",
        description="Generate the synthetic failed-payment dataset.",
    )
    parser.add_argument("--seed", type=int, default=42, help="RNG seed (default: 42)")
    parser.add_argument("--n", type=int, default=800, help="failed transactions (default: 800)")
    parser.add_argument(
        "--customers", type=int, default=300, help="customers to simulate (default: 300)"
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory")
    args = parser.parse_args(argv)

    paths = generate(args.seed, args.n, args.customers, args.out)
    for label in ("observed", "oracle", "splits", "summary"):
        print(f"wrote {paths[label]}")
    print(f"observed fields: {len(OBSERVED_FIELDS)}; withheld: {len(FORBIDDEN_OBSERVED_FIELDS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
