# Retry Economist — build progress

Failed-payment recovery agent that treats every retry as a purchase decision.
Razorpay AI Buildathon, Track 03 (AI Revenue Recovery).

Build order is deliberate: **the scoreboard exists before the agent**, so every
claim is measured against three baselines rather than asserted.

| Phase | Status |
| --- | --- |
| 0 — Setup, scope, repo structure | ✅ COMPLETE |
| 1 — Synthetic data generator + counterfactual oracle | ✅ COMPLETE |
| 2 — Evaluation harness (the scoreboard) | ✅ COMPLETE |
| 3 — Three baselines | ✅ COMPLETE |
| 4 — Three-signal router (LLM) | ⬜ NOT STARTED — **NEXT** |
| 5 — Economist layer (approve / veto) | ⬜ NOT STARTED |
| 6 — Bounded execution + audit trail | ⬜ NOT STARTED |
| 7 — Demo, README, architecture diagram | ⬜ NOT STARTED |

---

## ✅ Phase 1 — Synthetic data generator + counterfactual oracle

2,500 failed Indian payment transactions across 769 distinct customers, 8 issuers,
8 failure modes, a 45-day calendar with modelled bank outages.

- `observed.jsonl` — 22 agent-visible fields. `oracle.jsonl` — 9 withheld latent
  fields plus the outcome of **all 9 actions** per transaction.
- Ground-truth `would_pay_anyway` label, so uplift is measurable rather than assumed.
- Common random numbers across actions: all 9 outcomes share one uniform draw, so
  the oracle ceiling is "the best action", not "the luckiest draw".
- Splits by hash of `customer_id`, never by transaction — no group leakage.
- Deterministic: byte-identical output across runs at a fixed seed.

Holdout: **749 transactions / 240 customer clusters**, ₹35,44,846 at risk.

```
python -m retry_economist.generator.cli --seed 42 --n 2500 --customers 900
```

## ✅ Phase 2 — Evaluation harness

Simulator + metrics + clustered bootstrap. No policy may read the oracle; an
AST-based leakage guard enforces this in the test suite.

- Compliance gate lives in the **simulator**, not in policies, so `violations: 0`
  is evidence rather than a claim.
- Seven-bucket attribution, split first on whether the policy acted at all:
  `incremental / cannibalised / wasted / futile` when it acted;
  `correct_restraint / correct_walkaway / missed_opportunity` when it abstained.
- Cost model priced off customer lifetime value (annoyance → churn), with a
  `--clv-sweep` reporting which conclusions survive changing the assumption.
- Both **count-weighted and rupee-weighted** recovery, uplift and buckets.
- Time-to-recovery: mean/median days and a `recovered <=72h` rate.
- Clustered bootstrap (2,000 iterations, resampling **customers**) and paired
  bootstrap for policy-vs-policy comparisons.
- Headline generator refuses to emit a sentence it cannot support.

Recoverable ceiling on holdout: **61.4%** within scheme attempt caps
(61.7% ignoring them — caps cost 0.27pp of recovery outright).

## ✅ Phase 3 — Three baselines

`do_nothing`, `naive_retry_3x`, `rules_only`, plus a clearly-labelled cheating
`oracle_best` reference bound. **91 tests pass.**

### Key verified result

> **`rules_only` recovered 47.9% vs `naive_retry_3x` 39.0%**
> **paired CI +8.95pp [+5.72, +12.50] — excludes zero.**

Headline as generated from the data:

```
Recovered 47.9% vs 39.0% for naive_retry_3x, using 60% fewer retry attempts.
```

| policy | recovery | uplift pp | acted | attempts | decision P / R / F1 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `do_nothing` | 23.4% | +0.0 | 0 | 0 | n/a / 0.0% / n/a |
| `naive_retry_3x` | 39.0% | +15.6 | 702 | 1,430 | 20.8% / 96.7% / 34.2% |
| `rules_only` | **47.9%** | **+24.6** | 610 | **568** | 31.0% / 83.6% / 45.2% |
| `oracle_best` (CHEATS) | 61.4% | +38.1 | 285 | 240 | 100% / 100% / 100% |

Paired bootstrap, same customers resampled in both arms:

| comparison | Δ net uplift pp | Δ decision F1 | supported |
| --- | --- | --- | --- |
| `rules_only` vs `naive_retry_3x` | +8.95 [+5.72, +12.50] | +0.110 [+0.067, +0.154] | yes |
| `rules_only` vs `do_nothing` | +24.57 [+21.61, +27.74] | +0.452 [+0.414, +0.492] | yes |

**Stated honestly, because it matters:**

- This headline comes from a **lookup table with no LLM in it**. Phases 4–5 must
  earn their place against this bar, and will be measured, not assumed.
- `rules_only` wins recovery but **loses on speed**: 25.9% recovered within 72h
  versus naive retry's 39.0% (median 2.09 days, mean 6.04). It buys part of its
  advantage by waiting for payday. Phase 5 will price this with a time discount.
- Part of the "60% fewer attempts" is structural: `rules_only` plans one action,
  `naive_retry_3x` fires a three-attempt ladder.
- At the earlier 800-transaction scale (89 clusters) the paired CIs **straddled
  zero** and the rupee-weighted ranking reversed. The dataset was scaled to 2,500
  for statistical power; both scales are asserted in the test suite.
- `naive_retry_3x` burns **124 debit attempts on hard declines** that no retry
  can ever clear.

---

## ⬜ Phase 4 — Three-signal router — NEXT

Root cause · issuer health now · customer liquidity timing. One cached LLM call
per transaction, structured JSON out. The router **proposes only** — a `Proposal`
is not a `Decision` and cannot execute. Ships an `llm_router_only` ablation
policy (no economist) so each component's contribution is isolated.

## ⬜ Phase 5 — Economist layer

Incremental expected value — `amount × (P(recover|act) − P(recover|abstain)) ×
discount(days) − retry cost − annoyance cost − compliance risk`. Positive acts,
negative waits or stops with the reason logged. Hard compliance veto on risk
declines regardless of expected value.

## ⬜ Phase 6 — Bounded execution + audit trail

## ⬜ Phase 7 — Demo, README, architecture diagram

---

## Reproducing

```
pip install -e .
python -m retry_economist.generator.cli --seed 42 --n 2500 --customers 900
python -m retry_economist.eval.cli --split holdout --policies do_nothing,naive_retry_3x,rules_only,oracle_best
python -m pytest tests -q
```
