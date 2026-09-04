"""The audit trail: one JSONL record per economist decision, and nothing else.

Phase 6 is deliberately narrow. This module does not execute anything against
a real payment gateway - there is no HTTP client here, and none should be
added; wiring a `Decision` to a real Razorpay call is explicitly out of scope
(see `README.md`). What this module gives instead is the thing an auditor,
a compliance reviewer, or a merchant's support team actually needs before any
execution layer could be trusted: a durable, line-by-line record of WHY a
rupee was or was not spent, readable without opening the code.

Two properties make the ledger auditable rather than merely a log:

- **Append-only.** `AuditLedger.append` opens the file in append mode and
  writes exactly one line; nothing already on disk is ever read back or
  rewritten. A test asserts this directly - a file's first N bytes are
  byte-identical before and after a second run appends to it.
- **Idempotent action keys.** Every action in an authorised plan gets a
  `sha256(txn_id, action, attempt_index)` key (see `idempotency_key`),
  mirroring `llm/cache.py::cache_key`'s pattern. The key depends on nothing
  but those three deterministic values, so replaying the same decision twice
  - the exact scenario a retry-on-timeout execution layer would hit - always
  produces the same key, which is what lets a future executor recognise "I
  have already tried to do this" instead of double-charging a customer.

One record is written per `EconomistDecision`, whether the verdict was
`approve`, `approve_truncated`, or `veto` - a veto is a decision too, and the
reason a merchant's money was NOT spent belongs in the trail exactly as much
as the reason it was.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from retry_economist.policies.base import ObservedTransaction

DEFAULT_LEDGER_PATH = Path("results/audit_ledger.jsonl")


def idempotency_key(txn_id: str, action: str, attempt_index: int) -> str:
    """A deterministic key for one authorised action.

    `attempt_index` is the action's position (0-based) within the FINAL
    authorised plan, not within whatever the plan source originally proposed
    - compliance may have removed actions ahead of it, and the key must
    identify the action that will actually run, not a slot in a plan that
    never executes. Two runs over the same transaction that authorise the
    same plan produce byte-identical keys, because the key is a pure hash of
    these three values and nothing else - no timestamp, no random component.
    """
    digest = hashlib.sha256()
    digest.update(txn_id.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(action.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(str(attempt_index).encode("utf-8"))
    return digest.hexdigest()


def _plan_source_info(plan_source: Any, txn: ObservedTransaction) -> dict[str, Any]:
    """What the plan source itself said, independent of the economist's verdict.

    Two shapes exist in this codebase. An LLM-backed plan source
    (`LLMRouterOnlyPolicy`) keeps a `proposals` dict of real `Proposal`
    objects - self-reported root cause, confidence, and the two probabilities
    the model itself estimated (Phase 4 found these lose to the historical
    prior on this project's data, which is exactly why the economist prices
    with the prior instead - but an auditor should still be able to see what
    the model itself claimed). A deterministic plan source (`RulesOnlyPolicy`,
    `NaiveRetry3xPolicy`) has no such dict and no self-reported probabilities
    at all; its only provenance is the reason string on the `Decision` it
    returns, fetched here directly since `EconomistOverPlan` does not persist
    it - `plan_source.decide()` is pure and deterministic (a cache hit for a
    cached LLM proposal, a lookup for a rule table), so calling it again after
    the run to recover this string costs nothing and changes nothing.
    """
    proposals = getattr(plan_source, "proposals", None)
    if proposals is not None and txn.txn_id in proposals:
        p = proposals[txn.txn_id]
        return {
            "rationale": p.rationale,
            "root_cause": p.root_cause,
            "root_cause_confidence": p.root_cause_confidence,
            "p_recover_if_act": p.p_recover_if_act,
            "p_recover_if_abstain": p.p_recover_if_abstain,
        }
    source_decision = plan_source.decide(txn)
    return {
        "rationale": source_decision.reason,
        "root_cause": None,
        "root_cause_confidence": None,
        "p_recover_if_act": None,
        "p_recover_if_abstain": None,
    }


def _provider_info(plan_source: Any) -> tuple[str, str | None]:
    """Provider label and pinned model, derived from the plan source itself
    rather than trusted from a caller - `retry_economist (prior)`'s plan
    source (`RulesOnlyPolicy`) has no provider at all, and the record should
    say so rather than silently carry whatever the caller happened to pass.
    """
    router = getattr(plan_source, "router", None)
    if router is None:
        return "deterministic (no LLM)", None
    model = getattr(router.provider, "model", None)
    return f"router:{model}" if model else "router:unknown-model", model


def build_record(
    txn: ObservedTransaction,
    decision: Any,
    signals: Any,
    *,
    policy_name: str,
    estimator_label: str,
    provider_label: str,
    model: str | None,
    plan_source_info: dict[str, Any],
    decided_at: datetime | None = None,
) -> dict[str, Any]:
    """One full record. `decision` is an `EconomistDecision`; `signals` is the
    `Signals` computed for `txn` by the same `SignalIndex` the policy decided
    with. Every argument here is already-computed data - this function only
    assembles it into the shape that goes on disk, so it never needs to
    import the router or the economist package itself.
    """
    when = decided_at or datetime.now(timezone.utc)
    authorised_plan = list(decision.plan)
    return {
        "txn_id": txn.txn_id,
        "decided_at": when.isoformat(),
        "policy": policy_name,
        "estimator": estimator_label,
        "provider": {"label": provider_label, "model": model},
        "signals": signals.to_dict(),
        "proposal": {
            "plan": list(decision.proposed_plan),
            "rationale": plan_source_info["rationale"],
            "root_cause": plan_source_info["root_cause"],
            "root_cause_confidence": plan_source_info["root_cause_confidence"],
            "p_recover_if_act": plan_source_info["p_recover_if_act"],
            "p_recover_if_abstain": plan_source_info["p_recover_if_abstain"],
        },
        "compliance": decision.compliance.to_dict(),
        "ev": None if decision.ev is None else decision.ev.to_dict(),
        "verdict": decision.verdict,
        "authorised_plan": authorised_plan,
        "reason": decision.reason,
        "idempotency_keys": [
            {
                "action": action,
                "attempt_index": i,
                "key": idempotency_key(txn.txn_id, action, i),
            }
            for i, action in enumerate(authorised_plan)
        ],
    }


@dataclass
class AuditLedger:
    """A flat JSONL file, opened in append mode and never rewritten.

    `path`'s parent directory is created on first write, so pointing this at
    a fresh checkout does not require a separate setup step. Every `append`
    call is one `open(..., "a")`, one line written, one `close` - there is no
    in-memory buffer that could lose a record if the process dies mid-run.
    """

    path: Path = field(default_factory=lambda: DEFAULT_LEDGER_PATH)

    def append(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")

    def read_all(self) -> list[dict[str, Any]]:
        """For tests and reporting only - the ledger itself never reads its
        own history back to decide anything, which is what keeps `append`
        the only way this file is ever touched."""
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


def audit_policy_run(
    ledger: AuditLedger,
    policy: Any,
    transactions: Sequence[ObservedTransaction],
    *,
    provider_label: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Write one record per decision an `EconomistOverPlan`-shaped policy
    already made over `transactions`.

    Call this AFTER `eval.simulator.run(policy, transactions, ...)`, not
    instead of it: this function does not decide anything itself, it only
    reads back `policy.decisions` (populated during `run()`) and
    `policy.index` / `policy.plan_source` to reconstruct the provenance of
    each one. `policy` is accepted structurally (duck-typed) rather than by
    import, so this module never needs to import `EconomistOverPlan` and
    cannot create a cycle with the `policies` package - it only needs
    `.name`, `.economist.estimator.label`, `.index.signals_for`,
    `.plan_source`, and `.decisions`.

    `provider_label` / `model` are optional overrides for the caller's own
    preferred wording (the eval CLI and the subsample script both already
    compute a descriptive label elsewhere); when omitted they are derived
    from `policy.plan_source` itself via `_provider_info`, which is always
    correct even if a caller passes nothing.
    """
    derived_label, derived_model = _provider_info(policy.plan_source)
    records: list[dict[str, Any]] = []
    for txn in transactions:
        decision = policy.decisions.get(txn.txn_id)
        if decision is None:
            continue  # policy never decided this transaction; nothing to audit
        signals = policy.index.signals_for(txn)
        record = build_record(
            txn,
            decision,
            signals,
            policy_name=policy.name,
            estimator_label=policy.economist.estimator.label,
            provider_label=provider_label or derived_label,
            model=model if model is not None else derived_model,
            plan_source_info=_plan_source_info(policy.plan_source, txn),
        )
        ledger.append(record)
        records.append(record)
    return records
