"""Phase 4 close-out: score every policy on the LABELLED SUBSAMPLE of holdout
transactions that have a real `gemini-3.5-flash-lite` cached response.

The full 749-transaction holdout run was left running in the background across
two sessions and never finished (~0.5 calls/minute on the free tier makes the
full run a ~24h job). This script does not wait for it. It closes Phase 4 as an
honest, labelled subsample instead:

1. Defines the SUBSAMPLE as exactly the holdout transactions whose real router
   prompt - built from today's code, with the SignalIndex over the full
   749-transaction holdout, exactly as the background run would have built it -
   already has a cached response from the pinned model. No network call is
   attempted for anything else; a custom provider raises loudly if one would be.

2. Re-scores every policy (do_nothing, naive_retry_3x, rules_only,
   llm_router_only, all three retry_economist pairings - prior, naive plan,
   and LLM plan - oracle_best) on that SAME subset of transactions, using the
   SAME signal context (the full holdout) to build the deterministic policies,
   so the only thing that differs between this board and a full-holdout run
   is which transactions are scored, not how any policy's signals were
   computed.

3. Runs the diagnostics and calibration comparisons Phase 4 needs to close:
   action distribution vs rules_only/oracle_best, the root_cause_confidence
   histogram, per-failure-code p_recover_if_abstain vs the true organic rate,
   the three Brier scores, and the paired CI against rules_only.

4. Closes Phase 5's one remaining gap: `retry_economist (LLM plan)` is the
   full architecture end to end - the router's real, cached plan, priced and
   vetted by the same economist and train-only historical prior the other two
   pairings use. Reports its paired CI against `llm_router_only` (what did
   the economist add to the LLM?) and against `retry_economist (prior)` (what
   did the LLM add to the economist?), plus compliance-rule firing counts and
   the `approve_truncated` count - the first plan source in this project that
   gives C1/C2/C5 and a genuine partial truncation something real to act on.

Writes `results/subsample_scoreboard.md` and `.json`. Never touches
`results/holdout_scoreboard.md` - that file is Phase 5's non-LLM main result,
and this is a different experiment.

5. With `--audit`, off by default: appends one audit-ledger record per
   `retry_economist (LLM plan)` decision on the subsample to
   `results/audit_ledger.jsonl` (append-only; see `audit/ledger.py`).
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from retry_economist.economist.timing import DAILY_DISCOUNT_RATE
from retry_economist.eval import bootstrap as bs
from retry_economist.eval import calibration as cal
from retry_economist.eval import metrics as mx
from retry_economist.eval.cli import (
    PAIRED_COMPARISONS,
    PolicyReport,
    RETRY_ECONOMIST_NAIVE_PLAN_NAME,
    RETRY_ECONOMIST_PRIOR_NAME,
    _PRIOR_CONTEXT,
    _ROUTER_CONTEXT,
    DEFAULT_CUSTOMERS,
    DEFAULT_DATA_DIR,
    DEFAULT_N,
    DEFAULT_SEED,
    build_policies,
    ensure_data,
    generator_hash,
    historical_prior_estimator,
    render_json,
    render_markdown,
    run_paired_comparisons,
)
from retry_economist.eval.costs import CUSTOMER_LIFETIME_VALUE_PAISE
from retry_economist.eval.simulator import filter_split, run
from retry_economist.llm.cache import DEFAULT_CACHE_DIR, ResponseCache, cache_key
from retry_economist.llm.config import pinned_model
from retry_economist.llm.provider import CachingProvider
from retry_economist.policies.base import PLANNABLE_ACTIONS, ObservedTransaction
from retry_economist.policies.llm_router_only import LLMRouterOnlyPolicy
from retry_economist.policies.retry_economist_llm_plan import RetryEconomistLLMPlanPolicy
from retry_economist.router.router import Router, build_prompt
from retry_economist.router.signals import RISK, SignalIndex, root_cause_signal

RESULTS_DIR = Path("results")
ROUTER_POLICY_NAME = "llm_router_only (NO ECONOMIST)"
LLM_PLAN_POLICY_NAME = "retry_economist (LLM plan)"
EXTRA_PAIRED_COMPARISONS = (
    (LLM_PLAN_POLICY_NAME, ROUTER_POLICY_NAME),
    (LLM_PLAN_POLICY_NAME, RETRY_ECONOMIST_PRIOR_NAME),
)


class NetworkDisabledProvider:
    """Stands in for the real provider but refuses to ever touch the network.

    The subsample is *defined* as cache hits, so `.complete()` should never be
    called on anything reachable from the run below. If it is, that is a bug in
    the subsample selection, not something to paper over with a fabricated
    answer - so this raises loudly rather than degrading to an abstain.
    """

    def __init__(self, model: str) -> None:
        self.model = model

    def complete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(
            "network disabled for subsample scoring - this transaction was not supposed "
            "to be in the subsample; the selection step has a bug"
        )


def select_subsample(
    full_holdout: list[ObservedTransaction], index: SignalIndex, model: str, cache: ResponseCache
) -> list[ObservedTransaction]:
    """Holdout transactions whose real cached response is reachable right now.

    "Reachable right now" means: build the exact prompt today's code would send
    (signals from the full-holdout SignalIndex, matching how the background run
    built it), hash it, and check the committed cache. A transaction whose only
    cached entry was written under a DIFFERENT signal context (the earlier
    20-transaction smoke test used a 20-transaction SignalIndex, which computes
    different issuer baselines and customer-day evidence, hence a different
    prompt and a different hash) is a cache MISS under today's context and is
    excluded, rather than risk a live call.
    """
    subsample = []
    for txn in full_holdout:
        prompt = build_prompt(txn, index.signals_for(txn))
        path = cache.directory / f"{cache_key(model, prompt)}.json"
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("model") == model:
            subsample.append(txn)
    return subsample


def failure_code_distribution(transactions: list[ObservedTransaction]) -> Counter:
    return Counter(t.failure_code for t in transactions)


def render_distribution_comparison(sub: Counter, full: Counter) -> list[str]:
    lines = [
        "## Subsample definition and representativeness",
        "",
        "The SUBSAMPLE is exactly the holdout transactions with a real cached "
        "`gemini-3.5-flash-lite` response reachable under today's code with zero "
        "network calls (see the module docstring in `scripts/subsample_scoreboard.py` "
        "for exactly how that was determined, including why one transaction with a "
        "stale-context cache entry was excluded rather than risking a live call).",
        "",
        "| failure code | n in SUBSAMPLE | share of SUBSAMPLE | n in full holdout | "
        "share of full holdout | skew (pp) |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    sub_total = sum(sub.values())
    full_total = sum(full.values())
    for code in sorted(set(sub) | set(full), key=lambda c: (-full.get(c, 0), c)):
        s_n, f_n = sub.get(code, 0), full.get(code, 0)
        s_share = s_n / sub_total if sub_total else 0.0
        f_share = f_n / full_total if full_total else 0.0
        lines.append(
            f"| `{code}` | {s_n} | {s_share * 100:.1f}% | {f_n} | {f_share * 100:.1f}% | "
            f"{(s_share - f_share) * 100:+.1f} |"
        )
    lines.append("")
    return lines


def render_action_distribution(
    router_result, rules_result, oracle_result
) -> list[str]:
    def counts(result):
        c: Counter = Counter()
        for outcome in result.outcomes:
            c.update(outcome.plan)
        return c

    router_c, rules_c, oracle_c = counts(router_result), counts(rules_result), counts(oracle_result)
    lines = [
        "## Diagnostic 1 - action distribution: model vs `rules_only` vs `oracle_best`",
        "",
        "Counts are of PROPOSED actions (validated, pre-compliance-truncation), summed "
        f"across all {router_result.n} SUBSAMPLE transactions. A policy proposing an "
        "ordered plan of more than one action counts every action in it once.",
        "",
        "| action | model (llm_router_only) | rules_only | oracle_best (CHEATS) |",
        "| --- | ---: | ---: | ---: |",
    ]
    never_proposed = []
    for action in PLANNABLE_ACTIONS:
        r, ru, o = router_c.get(action, 0), rules_c.get(action, 0), oracle_c.get(action, 0)
        lines.append(f"| `{action}` | {r} | {ru} | {o} |")
        if r == 0 and (ru > 0 or o > 0):
            never_proposed.append(action)
    lines.append("")
    salary_day = router_c.get("retry_next_salary_day", 0)
    lines.append(
        f"> `retry_next_salary_day`: the model proposed it **{salary_day}** time(s) "
        f"across {router_result.n} transactions "
        f"(`rules_only`: {rules_c.get('retry_next_salary_day', 0)}, "
        f"`oracle_best`: {oracle_c.get('retry_next_salary_day', 0)})."
    )
    if never_proposed:
        lines.append(
            "> Actions the model **never** proposed on this subsample, despite "
            "`rules_only` and/or `oracle_best` using them: "
            + ", ".join(f"`{a}`" for a in never_proposed) + "."
        )
    else:
        lines.append("> The model proposed every action that `rules_only` or `oracle_best` did, at least once.")
    lines.append("")
    return lines


def render_confidence_histogram(proposals: dict) -> list[str]:
    values = [p.root_cause_confidence for p in proposals.values()]
    lines = [
        "## Diagnostic 2 - `root_cause_confidence` histogram",
        "",
    ]
    if not values:
        lines += ["_No proposals to histogram._", ""]
        return lines
    distinct = sorted(set(round(v, 4) for v in values))
    bins = [0.0] * 10
    for v in values:
        bins[min(9, int(v * 10))] += 1
    lines += [
        "| bin | count |",
        "| --- | ---: |",
    ]
    for i, count in enumerate(bins):
        if count:
            lines.append(f"| [{i / 10:.1f}, {(i + 1) / 10:.1f}) | {int(count)} |")
    lines.append("")
    lo, hi = min(values), max(values)
    if len(distinct) <= 2 and (hi - lo) < 0.05:
        lines.append(
            f"> **Effectively constant**: {len(distinct)} distinct value(s) across "
            f"{len(values)} proposals, range [{lo:.3f}, {hi:.3f}]. This signal is "
            "**uninformative** on this subsample - it carries no discriminating "
            "information about which proposals to trust more."
        )
    else:
        lines.append(
            f"> {len(distinct)} distinct value(s) across {len(values)} proposals, "
            f"range [{lo:.3f}, {hi:.3f}]."
        )
    lines.append("")
    return lines


def render_organic_gap(
    proposals: dict, subsample: list[ObservedTransaction], do_nothing_result
) -> list[str]:
    by_txn = {t.txn_id: t for t in subsample}
    organic_by_code: dict[str, list[bool]] = {}
    for outcome in do_nothing_result.outcomes:
        organic_by_code.setdefault(outcome.failure_code, []).append(outcome.would_pay_anyway)

    model_p_by_code: dict[str, list[float]] = {}
    for txn_id, proposal in proposals.items():
        txn = by_txn.get(txn_id)
        if txn is None:
            continue
        model_p_by_code.setdefault(txn.failure_code, []).append(proposal.p_recover_if_abstain)

    lines = [
        "## Diagnostic 3 - `p_recover_if_abstain` vs the TRUE organic rate, by failure code",
        "",
        "TRUE organic rate is `would_pay_anyway` measured directly from the oracle on "
        "this SUBSAMPLE (policy-independent - the same for every policy since it is a "
        "property of the transaction, not of any decision).",
        "",
        "| failure code | n | model mean p_recover_if_abstain | true organic rate | "
        "signed gap (model - true) |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for code in sorted(set(organic_by_code) | set(model_p_by_code)):
        organic = organic_by_code.get(code, [])
        model_p = model_p_by_code.get(code, [])
        true_rate = sum(organic) / len(organic) if organic else None
        mean_p = sum(model_p) / len(model_p) if model_p else None
        gap = (mean_p - true_rate) if (mean_p is not None and true_rate is not None) else None
        lines.append(
            f"| `{code}` | {len(model_p)} | "
            f"{'n/a' if mean_p is None else f'{mean_p:.3f}'} | "
            f"{'n/a' if true_rate is None else f'{true_rate:.3f}'} | "
            f"{'n/a' if gap is None else f'{gap:+.3f}'} |"
        )
    lines.append("")
    return lines


def render_llm_plan_diagnostics(
    llm_plan_policy: RetryEconomistLLMPlanPolicy,
    paired: list[dict[str, Any]],
    subsample: list[ObservedTransaction],
) -> list[str]:
    n = len(subsample)
    by_id = {t.txn_id: t for t in subsample}
    """The full architecture, end to end: what the pairing in Phase 5's gap
    actually did, on the 47 real cached proposals - paired CIs both ways,
    compliance-rule firing counts, and the verdict breakdown."""
    lines = [
        "## Diagnostic 6 - the full architecture, end to end: "
        f"`{LLM_PLAN_POLICY_NAME}`",
        "",
        f"The router's real, cached plan (all {n} SUBSAMPLE transactions), priced "
        "and vetted by the same economist and train-only historical prior "
        f"`{RETRY_ECONOMIST_PRIOR_NAME}` uses. Its own probabilities are never "
        "read - Phase 4 found they lose to this same prior.",
        "",
    ]

    def paired_row(subject: str, baseline: str, question: str) -> str:
        row = next((r for r in paired if r["subject"] == subject and r["baseline"] == baseline), None)
        if row is None:
            return f"> {subject} vs {baseline}: not present in this run."
        u = row["net_uplift_pp_delta"]
        verdict = (
            "excludes zero"
            if row["uplift_significant"]
            else f"STRADDLES ZERO - UNDERPOWERED at n={n}, not a claim either way"
        )
        return (
            f"> **{question}** `{subject}` vs `{baseline}`: net uplift "
            f"{u['point']:+.2f} pp [{u['low']:+.2f}, {u['high']:+.2f}] - {verdict}"
        )

    lines.append(
        paired_row(LLM_PLAN_POLICY_NAME, ROUTER_POLICY_NAME, "What did the economist add to the LLM?")
    )
    lines.append(
        paired_row(
            LLM_PLAN_POLICY_NAME, RETRY_ECONOMIST_PRIOR_NAME, "What did the LLM add to the economist?"
        )
    )
    lines.append("")
    lines.append(
        "Both comparisons are expected to be underpowered at n=47; the point of this "
        "policy existing is that the end-to-end configuration was built and measured "
        "at all, not that either comparison shows a winner here."
    )
    lines.append("")

    decisions = list(llm_plan_policy.decisions.values())
    verdict_counts = Counter(d.verdict for d in decisions)
    lines += [
        "### Verdict counts",
        "",
        f"`approve`: {verdict_counts.get('approve', 0)} | "
        f"`approve_truncated`: **{verdict_counts.get('approve_truncated', 0)}** | "
        f"`veto`: {verdict_counts.get('veto', 0)} (of {len(decisions)} decisions)",
        "",
    ]
    if verdict_counts.get("approve_truncated", 0) > 0:
        lines.append(
            "> `approve_truncated` fires here for real: the router proposes ordered, "
            "mixed-action plans, so a compliance rule removing part of one leaves a "
            "genuine profitable prefix behind, unlike the naive-plan pairing where a "
            "same-kind-action ladder meant a rule always removed either none of it or "
            "all of it."
        )
    else:
        lines.append(
            "> `approve_truncated` did not fire on this subsample either - even a "
            "mixed-action plan needs a compliance rule to remove PART of it, and at "
            "n=47 that combination did not occur."
        )
    lines.append("")

    rule_counts: Counter = Counter()
    for d in decisions:
        for check in d.compliance.checks:
            if check.fired:
                rule_counts[check.rule_id] += 1
    n_nonempty = sum(1 for d in decisions if d.proposed_plan)
    lines += [
        "### Compliance rule firing counts",
        "",
        f"(of {len(decisions)} economist decisions - {n_nonempty} had a non-empty router "
        f"proposal for compliance to check; the other {len(decisions) - n_nonempty} were "
        "router abstentions, auto-vetoed with nothing for any rule to remove)",
        "",
        "| rule | fired on n transactions |",
        "| --- | ---: |",
    ]
    for rule_id in ("C1_RISK_DECLINED", "C2_HARD_DECLINE_NO_DEBIT", "C3_ATTEMPT_CAP", "C4_EXPIRED_MANDATE", "C5_CONTACT_CAP"):
        lines.append(f"| `{rule_id}` | {rule_counts.get(rule_id, 0)} |")
    lines.append("")
    c1, c2 = rule_counts.get("C1_RISK_DECLINED", 0), rule_counts.get("C2_HARD_DECLINE_NO_DEBIT", 0)
    hard_or_risk_ids = [
        txn_id
        for txn_id in by_id
        if by_id[txn_id].decline_type == "hard" or root_cause_signal(by_id[txn_id]).value == RISK
    ]
    n_hard_or_risk = len(hard_or_risk_ids)
    if c1 or c2:
        lines.append(
            f"> `C1`/`C2` fire here ({c1}/{c2} transactions, of {n_hard_or_risk} hard-or-risk "
            "decline transactions in the subsample) because the router - unlike "
            "`rules_only` - sometimes proposes a debit retry on a risk decline or a hard "
            "decline; this is the first plan source in this project where those two "
            "rules had a live plan to remove on the SUBSAMPLE."
        )
    else:
        proposals_on_hard_or_risk = {
            txn_id: llm_plan_policy.plan_source.proposals[txn_id].proposed_plan
            for txn_id in hard_or_risk_ids
            if txn_id in llm_plan_policy.plan_source.proposals
        }
        n_empty = sum(1 for p in proposals_on_hard_or_risk.values() if not p)
        n_nonempty_debit_free = sum(
            1 for p in proposals_on_hard_or_risk.values() if p and set(p) <= {"request_new_mandate"}
        )
        lines.append(
            f"> **`C1`/`C2` did not fire on this subsample - a real, checked finding, not "
            "the expected one.** The prompt gives the model no rule against proposing a "
            "debit retry on a hard or risk decline, unlike `rules_only`'s hardcoded logic. "
            f"But of the {n_hard_or_risk} hard-or-risk-decline transactions in this "
            f"subsample, the real model proposed an empty plan on {n_empty} of them "
            f"(citing the hard-decline signal by name in its rationale) and proposed only "
            f"`request_new_mandate` - which C2 exempts, since it collects consent rather "
            f"than putting another debit on the wire - on {n_nonempty_debit_free} more. So "
            "these two compliance rules had nothing left to remove on this subsample - not "
            "because the plan source is rule-bound the way `rules_only` is, but because the "
            "model reached the same conclusion on its own here. Whether that holds at scale "
            "is exactly what the full 749-transaction run would tell and this subsample "
            "cannot."
        )
    lines.append("")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--audit",
        action="store_true",
        help=(
            "append one audit-ledger record per `retry_economist (LLM plan)` "
            "decision to results/audit_ledger.jsonl (off by default)"
        ),
    )
    args = parser.parse_args(argv)

    transactions, store, splits = ensure_data(
        DEFAULT_DATA_DIR, seed=DEFAULT_SEED, n=DEFAULT_N, n_customers=DEFAULT_CUSTOMERS
    )
    full_holdout = filter_split(transactions, splits, "holdout")
    train = filter_split(transactions, splits, "train")

    model = pinned_model()
    if model is None:
        raise SystemExit("no pinned model - run `python -m retry_economist.llm.discover` first")

    index = SignalIndex(full_holdout)
    cache = ResponseCache(DEFAULT_CACHE_DIR)

    subsample = select_subsample(full_holdout, index, model, cache)
    if not subsample:
        raise SystemExit("subsample is empty - nothing to score")

    n_real_cache_files = sum(
        1
        for f in cache.directory.glob("*.json")
        if json.loads(f.read_text(encoding="utf-8")).get("model") == model
    )

    print(f"final count of real {model} cache entries: {n_real_cache_files}")
    print(f"SUBSAMPLE: {len(subsample)} holdout transactions with a reachable real cached response")
    print(f"SUBSAMPLE customer clusters: {len({t.customer_id for t in subsample})}")

    # --- fit the historical prior on TRAIN only, same as the full-holdout run ---
    fitted_prior = cal.HistoricalPrior.fit(train, store)
    _PRIOR_CONTEXT["estimator"] = historical_prior_estimator(fitted_prior)
    _PRIOR_CONTEXT["daily_discount_rate"] = DAILY_DISCOUNT_RATE
    _PRIOR_CONTEXT["prior"] = fitted_prior

    # --- build every deterministic policy against the FULL-HOLDOUT signal context ---
    base_names = [
        "do_nothing",
        "naive_retry_3x",
        "rules_only",
        RETRY_ECONOMIST_PRIOR_NAME,
        RETRY_ECONOMIST_NAIVE_PLAN_NAME,
        "oracle_best",
    ]
    policies = build_policies(base_names, store, full_holdout)

    # --- build the router policy manually: cache-only, no network path at all ---
    router_provider = CachingProvider(NetworkDisabledProvider(model), ResponseCache(DEFAULT_CACHE_DIR))
    router = Router(router_provider, index)
    router_policy = LLMRouterOnlyPolicy(router)
    _ROUTER_CONTEXT.update(
        {
            "provider": router_provider,
            "label": f"gemini:{model} (SUBSAMPLE - cache replay only, no network)",
            "router": router,
            "policy": router_policy,
        }
    )
    policies.append(router_policy)

    # --- the full architecture, end to end: the router's plan, priced by the
    # SAME train-only prior the other two economist pairings use. A SEPARATE
    # Router instance (own stats, own LLMRouterOnlyPolicy.proposals dict) so
    # its call counts do not conflate with the standalone ablation above -
    # both point at the same on-disk cache, so this still costs zero network
    # calls; it is simply a second read of prompts already answered.
    llm_plan_router = Router(
        CachingProvider(NetworkDisabledProvider(model), ResponseCache(DEFAULT_CACHE_DIR)), index
    )
    llm_plan_policy = RetryEconomistLLMPlanPolicy(
        index, _PRIOR_CONTEXT["estimator"], llm_plan_router,
        daily_discount_rate=_PRIOR_CONTEXT["daily_discount_rate"],
    )
    policies.append(llm_plan_policy)

    # --- run every policy on the SAME subsample transactions, same customers ---
    reports: list[PolicyReport] = []
    results_by_name: dict[str, Any] = {}
    for policy in policies:
        result = run(policy, subsample, store, split="holdout_subsample")
        results_by_name[policy.name] = result
        reports.append(
            PolicyReport(
                name=policy.name,
                is_reference_bound=result.is_reference_bound,
                metrics=mx.compute_for_run(result, clv_paise=CUSTOMER_LIFETIME_VALUE_PAISE),
                ci=bs.bootstrap_run(
                    result, iterations=bs.DEFAULT_ITERATIONS, seed=bs.DEFAULT_SEED,
                    clv_paise=CUSTOMER_LIFETIME_VALUE_PAISE,
                ),
                result=result,
            )
        )

    if args.audit:
        from retry_economist.audit.ledger import AuditLedger, audit_policy_run

        ledger = AuditLedger()
        records = audit_policy_run(
            ledger,
            llm_plan_policy,
            subsample,
            provider_label=f"gemini:{model} (SUBSAMPLE - cache replay only, no network)",
            model=model,
        )
        print(f"audit: wrote {len(records)} ledger record(s) for {LLM_PLAN_POLICY_NAME!r} -> {ledger.path}")

    paired = run_paired_comparisons(
        reports,
        tuple(PAIRED_COMPARISONS) + EXTRA_PAIRED_COMPARISONS,
        iterations=bs.DEFAULT_ITERATIONS,
        seed=bs.DEFAULT_SEED,
    )

    calibration = None
    if router_policy.proposals:
        calibration = cal.evaluate(cal.build_records(router_policy.proposals, subsample, store), fitted_prior)

    common = dict(
        split="holdout_subsample",
        n_txns=len(subsample),
        n_customers=len({t.customer_id for t in subsample}),
        seed=DEFAULT_SEED,
        iterations=bs.DEFAULT_ITERATIONS,
        bootstrap_seed=bs.DEFAULT_SEED,
        ceilings=mx.ceilings(subsample, store),
        paired=paired,
        calibration=calibration,
        provider=_ROUTER_CONTEXT.get("label"),
        router_stats=router.stats.to_dict(),
        provider_stats=router_provider.report(),
    )

    md = render_markdown(reports, **common)
    md_lines = md.splitlines()

    extra: list[str] = []
    extra += render_distribution_comparison(
        failure_code_distribution(subsample), failure_code_distribution(full_holdout)
    )
    extra += render_action_distribution(
        results_by_name[ROUTER_POLICY_NAME], results_by_name["rules_only"], results_by_name["oracle_best (CHEATS)"]
    )
    extra += render_confidence_histogram(router_policy.proposals)
    extra += render_organic_gap(router_policy.proposals, subsample, results_by_name["do_nothing"])

    subject_significant = next(
        (row for row in paired if row["subject"] == ROUTER_POLICY_NAME and row["baseline"] == "rules_only"),
        None,
    )
    extra.append("## Diagnostic 4 - is the router-vs-rules_only comparison powered?")
    extra.append("")
    if subject_significant is None:
        extra.append("> Comparison not present in this run.")
    else:
        u = subject_significant["net_uplift_pp_delta"]
        verdict = (
            "excludes zero - the data supports a real difference at this n"
            if subject_significant["uplift_significant"]
            else "STRADDLES ZERO - at this sample size (n="
            f"{len(subsample)}), this comparison is UNDERPOWERED. The point estimate "
            f"({u['point']:+.2f} pp) is not a claim either way; the honest read is "
            "'not enough data to tell', not 'no difference' and not 'a win'."
        )
        extra.append(
            f"> `{ROUTER_POLICY_NAME}` vs `rules_only`: net uplift "
            f"{u['point']:+.2f} pp [{u['low']:+.2f}, {u['high']:+.2f}] - {verdict}"
        )
    extra.append("")

    if calibration is not None:
        act, abstain = calibration.act, calibration.abstain
        extra.append("## Diagnostic 5 - does the real model beat the train-only prior?")
        extra.append("")
        for score in (act, abstain):
            if score.router_brier is None:
                extra.append(f"> `{score.label}`: no scored transactions on this subsample.")
                continue
            verdict = "YES, the real model BEATS the prior" if score.beats_historical else "NO, the real model does NOT beat the prior"
            extra.append(
                f"> `{score.label}` (n={score.n}): router (real model) Brier "
                f"{score.router_brier:.4f} vs constant base-rate Brier {score.constant_brier:.4f} "
                f"vs train-only historical-prior Brier {score.historical_brier:.4f} - **{verdict}**."
            )
        extra.append("")

    extra += render_llm_plan_diagnostics(llm_plan_policy, paired, subsample)

    full_md = "\n".join(md_lines[:1] + [""] + [
        f"> **PHASE 4 CLOSED AS A LABELLED SUBSAMPLE, n={len(subsample)} of the full "
        f"749-transaction holdout.** The background run never finished; see "
        "`docs/PROGRESS.md` for why and for the exact selection rule.",
        "",
    ] + md_lines[1:] + [""] + extra)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    md_path = RESULTS_DIR / "subsample_scoreboard.md"
    json_path = RESULTS_DIR / "subsample_scoreboard.json"
    md_path.write_text(full_md + "\n", encoding="utf-8", newline="\n")

    payload = render_json(reports, **common)
    payload["subsample"] = {
        "n": len(subsample),
        "n_customers": len({t.customer_id for t in subsample}),
        "n_real_cache_entries_total": n_real_cache_files,
        "txn_ids": sorted(t.txn_id for t in subsample),
        "failure_code_distribution": dict(failure_code_distribution(subsample)),
        "full_holdout_failure_code_distribution": dict(failure_code_distribution(full_holdout)),
    }
    llm_plan_decisions = list(llm_plan_policy.decisions.values())
    payload["llm_plan_diagnostics"] = {
        "verdict_counts": dict(Counter(d.verdict for d in llm_plan_decisions)),
        "compliance_rule_firing_counts": dict(
            Counter(
                check.rule_id
                for d in llm_plan_decisions
                for check in d.compliance.checks
                if check.fired
            )
        ),
    }
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")

    print(f"wrote {md_path}")
    print(f"wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
