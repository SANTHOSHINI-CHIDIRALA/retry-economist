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
| 4 — Three-signal router | ✅ COMPLETE (SUBSAMPLE, n=47) — real `gemini-3.5-flash-lite`, not the full 749-transaction holdout; background run never finished, see note below |
| 5 — Economist layer (approve / veto) | ✅ COMPLETE, including the full architecture end to end (`retry_economist (LLM plan)`) - scored on Phase 4's SUBSAMPLE (n=47), not the full holdout; see "The full architecture, end to end" below. |
| 6 — Audit trail (execution deliberately out of scope) | ✅ COMPLETE |
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

## ✅ Phase 4 — Three-signal router — COMPLETE (SUBSAMPLE, n=47 of 749 holdout; the background run never finished the full holdout and this section reports the 47 it did finish, not the full 749)

The router **proposes only**. A `Proposal` is a distinct type from `Decision`, the
simulator accepts only a `Decision`, and a test walks the router's syntax tree to
assert it never constructs one. Turning a proposal into something executable
happens in three visible lines inside the ablation policy, nowhere else.

### STATUS UPDATE — 2026-09-05 — closed as a labelled subsample

`GEMINI_API_KEY` is set. `python -m retry_economist.llm.discover` ran for
real against the live API (not simulated) and pinned **`gemini-3.5-flash-lite`**,
chosen as the cheapest flash-tier model with structured JSON output among 11
candidates the API actually listed (full list, and the rejected candidates,
in `src/retry_economist/llm/model_pin.json`, committed).

**The full 749-transaction holdout run never finished.** It was left running
in the background across two sessions against a free-tier capacity limit of
roughly 0.5 calls/minute (see the rate-limit diagnosis below), which puts a
full run at roughly 24 hours. The background process died with the session
that started it and was not restarted. Rather than wait further, Phase 4 is
closed here as an honest, labelled **SUBSAMPLE** instead of a full-holdout
result.

**Final count of real `gemini-3.5-flash-lite` cache entries: 67 files** in
`data/llm_cache/`. Of those, 48 distinct holdout transaction ids are
represented; 19 of those are duplicated under two different prompt hashes
(an earlier 20-transaction smoke test built its `SignalIndex` over just those
20 transactions, which computes different issuer baselines and customer-day
evidence than the full-749 context the real run used later - a different
signal context is a different prompt, hence a different cache key for the
"same" transaction). One further transaction's only cached entry survives
under that stale, 20-transaction context and is not reachable from today's
full-holdout signal context, so it is excluded rather than risking a live
call. That leaves:

> **SUBSAMPLE = 47 holdout transactions** whose real cached response is
> reachable, right now, with **zero network calls**, by rebuilding the exact
> prompt today's code sends (signals from a `SignalIndex` over the full
> 749-transaction holdout - the same context the background run used).
> **41 distinct customer clusters.** Selection logic and the full scoring run
> are in `scripts/subsample_scoreboard.py`; full output in
> `results/subsample_scoreboard.md` / `.json`.

**Representativeness - stated rather than hidden.** The subsample is skewed
relative to the full 749-transaction holdout, because it is exactly the
chronological *first* ~97 transactions the background run reached before it
was interrupted, not a random draw:

| failure code | share of SUBSAMPLE (n=47) | share of full holdout (n=749) | skew |
| --- | ---: | ---: | ---: |
| `51` (insufficient funds) | 29.8% | 48.1% | **-18.3 pp** |
| `91` (bank downtime) | 27.7% | 17.2% | +10.4 pp |
| `41` (hard decline) | 14.9% | 7.6% | +7.3 pp |
| `MANDATE_EXPIRED_M06` | 8.5% | 4.8% | +3.7 pp |
| `U69` | 6.4% | 4.8% | +1.6 pp |
| `96` | 4.3% | 7.5% | -3.2 pp |
| `R05` (risk) | 4.3% | 5.5% | -1.2 pp |
| `ACS_TIMEOUT` | 4.3% | 4.5% | -0.3 pp |

Insufficient-funds is undersampled by 18pp and bank-downtime / hard-decline are
oversampled by 7-10pp each. Every number below is a SUBSAMPLE number, not a
full-holdout estimate, and should not be read as one.

A 20-transaction smoke test (the first 20 of this subsample) ran against the
real model early on. Rationales came back grounded in the specific
`failure_code` / `gateway_message` per transaction rather than boilerplate -
e.g. one proposal cited a 2.2x issuer failure-volume spike by name, another
cited the customer's own `past_success_rate`.

The architecture is complete and unchanged either way — only the source of the
proposals and probabilities differs. Adding a key requires two commands:

```
python -m retry_economist.llm.discover     # lists models, pins the cheapest flash tier
python -m retry_economist.eval.cli --split holdout --policies ...
```

**No model id is hardcoded anywhere.** `discover` lists what the API actually
offers, filters to flash-tier models supporting structured JSON output, excludes
preview/experimental variants, picks the cheapest and writes the exact string to
`llm/model_pin.json` with the rejected candidates recorded alongside.
`GeminiProvider` refuses to start without that pin rather than guessing a name.

### Step 0 — the futile split (done first, and it redirected the phase)

`futile` now splits by whose mistake it was:

| bucket | meaning | whose fault |
| --- | --- | --- |
| `futile_hopeless` | acted, and no affordable action would ever have worked | economics |
| `wrong_action` | acted on something recoverable, chose the wrong action | routing |

Plus `action_selection_error_rate` = wrong_action / (incremental + wrong_action),
and `addressable_capture_rate` against a denominator fixed by the data.

**This number decided where the remaining effort belongs.** Of `rules_only`'s 421
unproductive actions, **362 (86%) should never have been actioned at all** and only
**59 (14%) were right-to-act, wrong-action**. The router can address the 14%; the
economist owns the 86%.

### The three signals — deterministic, observed-data only

| signal | how | honesty |
| --- | --- | --- |
| `root_cause` | normalises `failure_code` + the messy `gateway_message` | reports **low confidence (0.30) on conflict** rather than picking one |
| `issuer_health_now` | this issuer's failure **volume** in a ±3h window vs its own all-feed baseline | never reads `world.py`; the feed holds only failures, so volume is the observable, not rate |
| `liquidity_timing` | infers payday from the day-of-month pattern of the customer's other failures | confidence 0.15 with no history, rising with evidence |

A unit test caught a real bug here: `"R05 -- SUSPECTED FRAUD, BLOCKED"` matched the
generic *blocked* pattern before the *fraud* pattern, misreading every risk decline
as a dead instrument and reporting a false conflict. Risk is now checked first.

### Reproducibility property

Every call goes through an on-disk cache keyed by `sha256(model + prompt)`. **The
cache is committed**, so the full evaluation replays offline with no API key.
On the SUBSAMPLE this is measured, not aspirational:

```
calls=47  network=0  cache_hits=47  hit_rate=100.0%
```

(`results/subsample_scoreboard.md`, "Router and provider" section.) The full
749-transaction holdout does **not** currently replay at 100%: only 47 of its
749 prompts have a real cached response, so a full-holdout run today would
need a network call - and the ~0.5 calls/minute capacity limit - for the
remaining 702.

A cache hit never opens a socket — asserted by a test with a call-counting
provider. No key material is ever written to disk; a test greps the cache for it.

### Rate limits abort; they never fake an abstention

429s retry with exponential backoff (1s, 2s, 4s, 8s, 16s). When the budget is
exhausted the run raises `RateLimited` and **stops**, rather than degrading to
an abstain proposal — a scoreboard full of "the router chose to do nothing" rows
that really mean "we never asked" would be a fabricated result.

Because every completed call is already cached, a rate-limited run resumes:
a test kills a 10-transaction run after 4 calls, then re-runs against the same
cache and asserts exactly 4 cache hits and 6 new calls.

### Rate-limit diagnosis — it is not a quota rejection at all

Reading the actual exceptions (a one-off diagnostic call with the `google-genai`
SDK's own internal auto-retry explicitly disabled, so a raw error surfaces
instead of being silently absorbed) found this is **not a 429 / quota problem**.
Every failure observed was:

```
code    = 503
message = 'This model is currently experiencing high demand. Spikes in
           demand are usually temporary. Please try again later.'
status  = 'UNAVAILABLE'
```

That is a **capacity signal (503 UNAVAILABLE), not a rate-limit rejection
(429 RESOURCE_EXHAUSTED)** — the model's free-tier compute is oversubscribed
right now, not "you have used your allotted requests." This distinction
matters for planning: it means none of requests-per-minute,
requests-per-day, or tokens-per-minute is the bottleneck in the sense the
question assumed. There is no daily counter to exhaust, so **"tomorrow will
not be faster" does not follow** the way it would for an RPD cap — but
neither is there a clean per-minute counter that resets predictably; shared
capacity congestion can persist, fluctuate, or clear at any time of day, and
is not something this project can measure from the client side. The one
number that is real: sustained throughput on this key, tonight, is
**~0.5 calls/minute**.

**A real bug this uncovered:** `is_rate_limit()` matched on `429` / `quota` /
`resource_exhausted` text only. A 503 never matched, so when the SDK's own
internal retry budget (5 attempts, its own backoff — separate from and in
addition to this project's own backoff loop) was exhausted, the exception
would have fallen through to the generic-API-error branch in
`GeminiProvider._attempt`, counted as a parse failure after one repair
attempt, and degraded to a **fabricated ABSTAIN proposal** — silently
misrepresenting "the provider was down when we asked" as "the model produced
nothing usable." Fixed in `llm/provider.py`: 503 / `UNAVAILABLE` / "high
demand" now get the identical backoff-then-`RateLimited` treatment as a 429,
with a regression test
(`tests/test_router.py::test_a_503_overload_is_treated_as_a_rate_limit_not_a_parse_failure`).
**This fix shipped in the code but not retroactively in the background run**,
which loaded the old module before the fix landed and was never restarted.
**Resolved by direct check rather than left as a caveat**: the SUBSAMPLE run
(`results/subsample_scoreboard.md`, "Router and provider") reports
`parse_failures=0` and `schema_violations=0` across all 47 real proposals -
none of the 13 empty-plan proposals in the subsample are a silently-fabricated
abstain from this gap; every one of them is the model genuinely returning an
empty `proposed_plan`.

### Degradation is one-directional

Unparseable output gets one repair attempt, then becomes an **ABSTAIN** proposal
and a counter. An action outside the allowed set, or a probability outside [0,1],
invalidates the whole response the same way. A malfunctioning model costs nothing
and can never spend a merchant's money. Three tests cover these paths.

### Result — the real model, on the SUBSAMPLE (n=47)

All numbers below are from `results/subsample_scoreboard.md`, produced by
`python scripts/subsample_scoreboard.py`, and are **real `gemini-3.5-flash-lite`
output, not a heuristic** - zero parse failures, zero schema violations,
0 network calls (100% cache hit rate; see the reproducibility section above).

Six-policy board on the SUBSAMPLE, same 47 transactions / 41 customers every arm:

| policy | recovery | uplift pp | acted | attempts | decision precision | decision F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `do_nothing` | 23.4% | +0.0 | 0 | 0 | n/a | n/a |
| `naive_retry_3x` | 40.4% | +17.0 | 44 | 86 | 20.5% | 34.0% |
| `rules_only` | **53.2%** | **+29.8** | 37 | 33 | 37.8% | 52.8% |
| `retry_economist (naive plan)` | 38.3% | +14.9 | 17 | 29 | 41.2% | 42.4% |
| `retry_economist (prior)` | 44.7% | +21.3 | 30 | 30 | 33.3% | 43.5% |
| `llm_router_only (NO ECONOMIST)` | 48.9% | +25.5 | 34 | 29 | 35.3% | 48.0% |
| `oracle_best` (CHEATS) | 61.7% | +38.3 | 18 | 13 | 100% | 100% |

**Paired, same customers both arms:**
`llm_router_only vs rules_only` = **-4.26 pp [-13.33, +4.17]** — straddles zero.

**This is an underpowered comparison, reported as one, not as a win or a
loss.** At n=47 (41 customer clusters), the confidence interval spans almost
18 percentage points and contains zero comfortably. The point estimate says
the real model recovered a bit less than `rules_only` on this particular
47-transaction slice; the interval says the data cannot tell the two apart at
this sample size. Neither "the model wins" nor "the model loses" is a
supportable sentence here — "not enough data yet" is the honest one.

**Null finding 1 (holds on real data too) — the plans mostly agree with rules,
but not entirely.** Action-by-action (`results/subsample_scoreboard.md`,
Diagnostic 1): the model proposes `retry_next_salary_day` **9** times against
`rules_only`'s 14 and `oracle_best`'s 3 — it leans on the liquidity-timing
signal, but less than the rules table does. It never proposes
`nudge_then_retry` at all on this subsample, though `rules_only` uses it
twice. Unlike the earlier mock run (whose playbook exactly duplicated the
rules table by construction), the real model's plan is **not** a copy of
`rules_only`'s — it disagrees on which action to take on some transactions,
which is what makes `wrong_action` (2, same value coincidentally as
`rules_only`'s) a real, measured quantity here rather than structurally zero.

**Null finding 2 — the probability estimates still lose to a lookup, and now
it's a real model losing, not a heuristic.** Scored as forecasts against a
per-failure-code prior fitted on the **train split only** (1,751 transactions;
no holdout customer contributes):

| estimate | n | router (real model) Brier | constant Brier | train-only prior Brier | beats prior? |
| --- | ---: | ---: | ---: | ---: | :---: |
| `p_recover_if_act` | 34 | 0.2780 | 0.2962 | **0.2449** | **no** |
| `p_recover_if_abstain` | 47 | 0.2026 | 0.1793 | **0.1587** | **no** |

**Stated plainly: the real model does NOT beat the train-only historical
prior on either estimate, on this subsample.** It beats a constant base rate
on `p_recover_if_act` but loses to it on `p_recover_if_abstain` too. This
answers the question the STAND-IN section could only leave open: a real
model, asked to estimate the same two probabilities, does not add information
over a simple per-failure-code lookup table here. n=47/34 is small enough
that this is not a final word on larger data, but it is what the real model
actually produced, not a projection.

**Diagnostic - `root_cause_confidence` is uninformative on this subsample.**
All 47 real proposals returned **exactly 0.95**, with zero parse failures or
schema violations to explain the lack of variation - the model did not
express any granularity of doubt across 47 genuinely different transactions
(hard declines, risk flags, bank downtime, insufficient funds all included).
This signal carries no discriminating information here and should not be
weighted by any downstream consumer until shown otherwise on more data.

**Diagnostic - `p_recover_if_abstain` vs the true organic rate, by failure
code** (full table in `results/subsample_scoreboard.md`, Diagnostic 3): gaps
range from -0.35 (`ACS_TIMEOUT`, n=2) to +0.15 (`91`/bank downtime, n=13), with
no consistent over- or under-estimation direction across codes - consistent
with the calibration result above rather than contradicting it.

Ten-bin reliability tables for both estimates are in
`results/subsample_scoreboard.md` and reproduce from the committed cache with
`python scripts/subsample_scoreboard.py` — no API key needed for anyone
re-running this exact result.

## ✅ Phase 5 — COMPLETE (historical-prior pairings on the full holdout; the LLM-priced pairing below is scored on Phase 4's SUBSAMPLE, n=47, not the full holdout)

Built in `src/retry_economist/economist/`: `EVTerms` (every term of the formula
itemised), five hard compliance rules (`C1`-`C5`, each able only to REMOVE an
action, never add or reorder one), and `Economist.decide()` returning
`approve` / `approve_truncated` / `veto` with a full audit trail. 178 tests
pass, including a property test per compliance rule with a ₹10-crore
transaction confirming EV never overrides one.

Two plan sources are wired to the SAME economist and estimator
(`src/retry_economist/policies/retry_economist_prior.py` /
`retry_economist_naive_plan.py`, sharing a common `EconomistOverPlan` base):
**`retry_economist (prior)`** prices `rules_only`'s plan; **`retry_economist
(naive plan)`** prices `naive_retry_3x`'s fixed three-attempt ladder instead.
The second exists specifically to isolate what the ECONOMIST alone
contributes: `rules_only` already discriminates by failure code before the
economist ever sees a plan, so the gap between it and `retry_economist
(prior)` understates the economist's own value. `naive_retry_3x` proposes the
identical ladder regardless of what failed - including on blocked cards and
risk declines - so every improvement over it is attributable to the economist
alone.

```
EV(plan) = amount_paise × VALUE_CAPTURE_RATE × delta_p × discount(days)
           − action costs − annoyance cost
delta_p  = p_recover_if_act − p_recover_if_abstain   (INCREMENTAL, never gross)
discount = 1 / (1 + DAILY_DISCOUNT_RATE) ** expected_days_to_recovery
DAILY_DISCOUNT_RATE = 0.02   # ASSUMPTION: working capital + invoice staleness
```

### Why "against the historical prior" and not the LLM, today

Phase 4's real holdout run cannot finish before this had to ship (~24h at the
free-tier rate; see the status update above). But an economist needs a PLAN to
price, and pricing one does not require that plan to come from an LLM:
`rules_only` already proposes a deterministic, observed-data-only plan for
every transaction. **`retry_economist (prior)`** (`policies/retry_economist_prior.py`)
takes `rules_only`'s plan unchanged and prices it with `HistoricalPriorEstimator`
- the same train-only per-failure-code prior Phase 4's calibration module
fits, unpacked into plain data so the economist package never imports
`retry_economist.eval` (the leakage guard forbids it structurally; see
`economist/estimator.py`'s docstring). This needs no API key, no cache, no
network, and scores the entire holdout in under 3 seconds. It is also a clean
test of the ECONOMIST alone: the plan side is held fixed at `rules_only`'s own
proposal, so any difference from plain `rules_only` is attributable to the
economics, not to a better plan. The LLM-priced pairing (router's plan +
either estimator) is deferred until Phase 4's run lands.

### Main result — six-policy holdout scoreboard, no LLM involved

| policy | recovery | uplift pp | decision P / R / F1 | attempts | cost INR | net value INR | INR/INR |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `do_nothing` | 23.4% | +0.0 | n/a / 0.0% / n/a | 0 | 0 | 0 | n/a |
| `naive_retry_3x` | 39.0% | +15.6 | 20.8% / 96.7% / 34.2% | 1,430 | 41,010 | 309,823 | 0.12 |
| `rules_only` | **47.9%** | **+24.6** | 31.0% / 83.6% / 45.2% | 568 | 30,813 | 684,974 | 0.04 |
| `retry_economist (naive plan)` | 33.0% | +9.6 | 28.2% / 33.3% / 30.6% | 513 | **14,917** | 322,066 | 0.04 |
| `retry_economist (prior)` | 45.4% | +22.0 | 32.4% / 73.0% / 44.9% | **515** | 17,018 | **688,983** | **0.02** |
| `oracle_best` (CHEATS) | 61.4% | +38.1 | 100% / 100% / 100% | 240 | 16,612 | 981,483 | 0.02 |

Paired bootstrap, same customers resampled both arms (2,000 iterations):

| comparison | Δ net uplift pp | Δ decision F1 | supported |
| --- | --- | --- | --- |
| `rules_only` vs `naive_retry_3x` | +8.95 [+5.72, +12.50] | +0.110 [+0.067, +0.154] | yes |
| `rules_only` vs `do_nothing` | +24.57 [+21.61, +27.74] | +0.452 [+0.414, +0.492] | yes |
| `retry_economist (prior)` vs `rules_only` | **−2.54 [−3.63, −1.52]** | −0.004 [−0.022, +0.015] | uplift: yes (economist recovers LESS) · F1: no |
| `retry_economist (prior)` vs `naive_retry_3x` | +6.41 [+3.25, +9.80] | +0.106 [+0.062, +0.154] | yes |
| `retry_economist (naive plan)` vs `naive_retry_3x` | **−6.01 [−8.53, −3.66]** | −0.037 [−0.075, +0.002] | uplift: yes (economist recovers LESS) · F1: no |

**Stated plainly, because it is the honest result:** paired against its OWN
plan source, `retry_economist (naive plan)` recovers **significantly less**
than `naive_retry_3x` (−6.01pp, CI excludes zero) - fully vetoing 415 of the
702 transactions naive proposed to act on (59%; 287 approved unchanged, none
truncated - see the verdict counts below) costs real recovery, not just
waste. But it also spends **64% less** (14,917 vs 41,010 INR) for a
**higher net value** (322,066 vs 309,823), because almost everything it
vetoed was either guaranteed to fail (hard/risk declines, expired mandates -
see below) or was economically not worth its cost. Against `rules_only`'s
plan the same trade appears smaller (−2.54pp, 45% less spend) because
`rules_only` had already filtered out most of the obviously-bad actions
itself; against `naive_retry_3x`'s blind plan the ECONOMIST's own contribution
is fully exposed, and it is larger in both directions - more recovery given
up, more spend avoided. This is EV-gating doing exactly what it is built to
do: decline actions that would have worked because, priced against the
historical prior's probabilities, they were not worth the expected
annoyance/attempt cost. Whether the trade is "worth it" depends on the CLV
assumption - see the sweep below - not on a single number.

### Sensitivity: does the trade survive changing the assumptions?

**CLV sweep** (net value, INR 4,000 / 12,000 / 30,000):

| policy | INR 4k | INR 12k (default) | INR 30k |
| --- | ---: | ---: | ---: |
| `naive_retry_3x` | 335,257 | 309,823 | 252,597 |
| `rules_only` | **704,596** | 684,974 | 640,824 |
| `retry_economist (prior)` | 699,601 | **688,983** | **665,094** |
| `oracle_best` (CHEATS) | 991,838 | 981,483 | 958,183 |

**The ranking FLIPS between `retry_economist (prior)` and `rules_only`** somewhere
between CLV 4,000 and 12,000: at low assumed lifetime value, `rules_only`'s
extra recovery wins on net value outright; at the default (12,000) and above,
the economist's lower spend wins. This is not noise - it says the "trade
raw recovery for spend efficiency" conclusion is CLV-dependent, and must not be
stated without naming the assumption.

**Discount-rate sweep** (0.5% / 2% / 5% daily; RE-DECIDED at each rate, not
merely re-priced - see `economist/economist.py::compute_ev`'s docstring for why
this cannot be a post-hoc reweighting the way the CLV sweep is):

| daily rate | net uplift pp |
| --- | ---: |
| 0.005 | +22.43 |
| 0.020 (default) | +22.03 |
| 0.050 | +21.36 |

- vs `naive_retry_3x` (+15.6pp): advantage **survives every rate tested**.
- vs `rules_only` (+24.6pp): advantage **does not survive any rate tested** -
  it was already behind at the default rate, and a higher discount rate (which
  penalises `retry_next_salary_day`'s wait more) only widens the gap.

### Veto precision — `retry_economist (prior)` (rules_only's plan)

Of `rules_only`'s 610 non-empty proposals, the economist fully approved 525,
truncated none, and fully vetoed 85 (139 more transactions had no proposal to
begin with - `rules_only` itself abstained). Full breakdown in
`results/veto_precision.md`.

| split | n vetoed actions | would have failed anyway | veto precision |
| --- | ---: | ---: | ---: |
| all vetoes | 85 | 43 | 50.6% |
| compliance-driven (`C5_CONTACT_CAP`) | 30 | 14 | 46.7% |
| economics-driven (`EV<=0`) | 55 | 29 | 52.7% |

**Only `C5_CONTACT_CAP` fired against `rules_only`'s plan** - `C1`, `C2`, `C3`
and `C4` never removed a single action there. Not a bug: `rules_only` already
abstains on every hard decline, risk decline and expired mandate itself, and
already respects the attempt cap before proposing anything, so those four
rules never see a live plan to veto under THIS pairing. The naive-plan pairing
below is what exposes them.

**Veto precision just above 50% is the honest headline of this sub-result**:
roughly half of what the economist vetoed on economic or contact-cap grounds
would, per the oracle, have recovered the payment. That is the real price of
the caution behind the −2.54pp uplift gap above - not a free lunch.

### The economist's isolated contribution — `retry_economist (naive plan)`

Pairing the SAME economist and estimator with `naive_retry_3x`'s
failure-code-blind ladder instead gives all five compliance rules real
ammunition, and gives a clean, plan-source-independent measurement of what the
economist itself is worth. Full breakdown in
`results/veto_precision_naive_plan.md`.

**Compliance rule firing counts** (of 702 non-empty `naive_retry_3x`
proposals; 47 more transactions had `attempts_left == 0` and no proposal at
all):

| rule | fired on n transactions |
| --- | ---: |
| `C1_RISK_DECLINED` | 39 |
| `C2_HARD_DECLINE_NO_DEBIT` | 54 |
| `C3_ATTEMPT_CAP` | 0 |
| `C4_EXPIRED_MANDATE` | 41 |
| `C5_CONTACT_CAP` | 0 |

`C1`, `C2` and `C4` are non-zero here, exactly because this plan source does
not discriminate the way `rules_only` does. `C3` and `C5` are still zero, for
two DIFFERENT, mechanical reasons rather than one: `naive_retry_3x` already
truncates its own ladder to `attempts_left` before proposing anything (see
`policies/naive_retry.py`), so `C3`'s double-guard never finds a plan over
budget; and the ladder is three retry actions only, none of which
`contacts_customer` (see `economist/costs.py`), so `C5` structurally has
nothing to remove regardless of `comms_received_last_7d`.

**Verdict counts**: 287 `approve`, **0 `approve_truncated`**, 415 `veto`.
`approve_truncated` still did not fire, and now for a reason confirmed by data
rather than assumed: `naive_retry_3x`'s ladder is three actions of the SAME
kind (debit retries), so `C1`/`C2`/`C4` always remove either none of it or ALL
of it - there is never a partial removal to truncate. A plan source that mixes
action types (a debit retry alongside a contact action, say) is what would
give a compliance rule something to remove PART of; the LLM router's plan is
the first candidate for that once Phase 4 lands.

**`hard_decline_retry_waste`** - debit attempts spent retrying an instrument
already classed as a hard decline, which no retry on any rail can ever clear:

| policy | hard_decline_retry_waste |
| --- | ---: |
| `naive_retry_3x` | **245** |
| `retry_economist (naive plan)` | **0** |

Given the IDENTICAL proposed ladder on every one of those transactions, this
245 → 0 delta is attributable entirely to `C1` and `C2` - the single clearest
demonstration in this project of what the economist is for. (Phase 3's
PROGRESS.md entry cites 124 hard-decline attempts; that figure predates this
measurement and its exact split/scale was not re-verified here, so it is
noted rather than reconciled - 245 is the number this run actually produced,
on the current 749-transaction holdout, computed by `eval/metrics.py`'s
existing `hard_decline_retry_waste` field.)

**Veto precision, compliance vs economics:**

| split | n vetoed actions | would have failed anyway | veto precision |
| --- | ---: | ---: | ---: |
| all vetoes | 1,115 | 796 | 71.4% |
| compliance-driven (`C1`+`C2`+`C4`) | 335 | 329 | **98.2%** |
| economics-driven (`EV<=0`) | 780 | 467 | 59.9% |

| rule | n vetoed actions | would have failed anyway | veto precision |
| --- | ---: | ---: | ---: |
| `C1_RISK_DECLINED` | 101 | 101 | **100.0%** |
| `C2_HARD_DECLINE_NO_DEBIT` | 144 | 144 | **100.0%** |
| `C4_EXPIRED_MANDATE` | 90 | 84 | 93.3% |
| `EV<=0` | 780 | 467 | 59.9% |

**The compliance rules are near-perfect (98.2% combined; `C1` and `C2` exactly
100%): everything they vetoed on this holdout would have failed anyway - they
cost nothing.** The economics gate is where the real trade-off lives: 59.9%
precision means roughly 4 in 10 economically-vetoed actions would actually
have recovered the payment. That is consistent with the −6.01pp uplift loss
above, and it says plainly where this policy's caution is expensive and where
it is free.

### The RISK_DECLINED veto demo — now with a real transaction

`results/veto_demo_real.txt` is the PRIMARY demo: a full trace, on a REAL
holdout transaction (`pay_00647`, INR 60,000, code `R05` /
`"R05 -- SUSPECTED FRAUD, BLOCKED"`), found via `retry_economist (naive plan)`
- `naive_retry_3x` proposes its ladder on this transaction regardless of the
risk decline, so `C1` has something real to veto. Reported exactly as
measured, including where it differs from what was expected going in: of the
39 real transactions where `C1` fired this way, **0 of 39 had a positive
hypothetical EV** against the real train-fitted historical prior (the traced
one: **−INR 61.60**). The prior has already learned that risk declines
essentially never recover, so on this holdout `C1` turned out to be redundant
with the economics rather than overriding it - a finding about how well the
prior generalises, not a gap in the demo. `results/veto_demo.txt` (the earlier
CONSTRUCTED transaction, primed with a deliberately optimistic estimator to
show a **+INR 18,711.78** hypothetical EV) is kept as a fallback specifically
because it is the one place a positive-EV override is shown explicitly; both
files cross-reference each other. The underlying guarantee - `C1` removes the
plan before any EV arithmetic runs, positive or not - is the same property
`tests/test_economist.py::test_c1_vetoes_a_risk_decline_at_any_expected_value`
asserts.

### The full architecture, end to end — `retry_economist (LLM plan)`

The one gap this project had left: the router proposing and the economist
approving/vetoing had each been measured, but never together, on real
transactions. `src/retry_economist/policies/retry_economist_llm_plan.py` closes
it - and it is thin, exactly as the shared `EconomistOverPlan` base was built
to allow. The router's real, cached plan for each of Phase 4's 47 SUBSAMPLE
transactions is priced by the SAME `HistoricalPriorEstimator` and the SAME
`Economist` that `retry_economist (prior)` and `retry_economist (naive plan)`
already use - the router's own `p_recover_if_act` / `p_recover_if_abstain` are
never read, because Phase 4 found they lose to this prior on both estimates.
Zero API calls: it runs entirely off the 47 cached real proposals, through a
second `Router` instance pointed at the same on-disk cache with a
network-disabled provider, so a coding mistake anywhere in this path would
raise loudly rather than place a live call.

Full table, calibration and diagnostics in `results/subsample_scoreboard.md`
("Diagnostic 6"), produced by `python scripts/subsample_scoreboard.py`
(offline, no key needed). The result on the SUBSAMPLE (n=47, 41 customers):

| policy | recovery | uplift pp | acted | attempts | decision precision | decision F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `retry_economist (prior)` | 44.7% | +21.3 | 30 | 30 | 33.3% | 43.5% |
| `llm_router_only (NO ECONOMIST)` | 48.9% | +25.5 | 34 | 29 | 35.3% | 48.0% |
| `retry_economist (LLM plan)` | 42.6% | +19.1 | 26 | 25 | 34.6% | 42.9% |

**Paired CIs, both directions, both underpowered - reported as such:**

- **What did the economist add to the LLM?** `retry_economist (LLM plan)` vs
  `llm_router_only`: **−6.38 pp [−13.73, +0.00]** - straddles zero (the
  interval's upper edge lands exactly on zero). At n=47 this cannot
  distinguish "the economist's vetoes cost real recovery here" from "no
  measurable difference at this sample size" - both are consistent with the
  data.
- **What did the LLM add to the economist?** `retry_economist (LLM plan)` vs
  `retry_economist (prior)`: **−2.13 pp [−9.52, +4.55]** - straddles zero,
  more comfortably. Same caveat: not enough data to say the LLM-sourced plan
  is worse than `rules_only`'s plan once both are priced by the same
  economist, only that this particular 47-transaction slice does not resolve
  it either way.

Neither result is a win or a loss for the end-to-end configuration. The
point of building it was that it now EXISTS and was measured on real
transactions, which is what closes the gap - not that either comparison
favours it at this n.

**Verdict counts**: 26 `approve`, **0 `approve_truncated`**,  21 `veto` (of 47
decisions). `approve_truncated` still did not fire, for the same structural
reason it hasn't fired anywhere yet in this project: even though the router
can propose ordered, mixed-action plans (unlike `naive_retry_3x`'s
same-kind ladder), a rule still needs a plan where PART of it should be
removed and part should survive, and at n=47 that combination did not occur.

**Compliance rule firing counts** (of 47 decisions; 34 had a non-empty router
proposal, the other 13 were router abstentions with nothing for any rule to
check):

| rule | fired on n transactions |
| --- | ---: |
| `C1_RISK_DECLINED` | 0 |
| `C2_HARD_DECLINE_NO_DEBIT` | 0 |
| `C3_ATTEMPT_CAP` | 0 |
| `C4_EXPIRED_MANDATE` | 0 |
| `C5_CONTACT_CAP` | 1 |

**A real finding, not the expected one.** The working assumption going in was
that C1/C2 would finally get real ammunition here, since nothing in the
router's prompt forbids proposing a debit retry on a hard or risk decline the
way `rules_only`'s hardcoded logic does. Checked directly against the 9
hard-or-risk-decline transactions in this subsample: the model proposed an
empty plan on 8 of them, citing the hard-decline signal by name in its own
rationale (e.g. *"hard declines ... can never be cleared by retrying"*), and
proposed only `request_new_mandate` - which C2 exempts, since it collects
consent rather than putting another debit on the wire - on the ninth. So C1
and C2 had nothing to remove on this subsample: not because the plan source
is rule-bound the way `rules_only` is, but because the real model
independently reasoned its way to the same restraint on every one of these 9
cases. This is a genuine property of the 47 cached responses, checked rather
than assumed, and it is exactly the kind of thing a larger sample could
still overturn - a model correct on 9 cases is not a model verified safe on
all of them.

### What's still open

- **The full architecture is now scored, but only on Phase 4's 47-transaction
  subsample.** Both paired comparisons above straddle zero, and the
  compliance-rule finding (C1/C2 never firing) is checked on 9 hard-or-risk
  transactions - real, but not enough to generalise. Re-running
  `retry_economist (LLM plan)` once more of the full 749-transaction holdout
  is cached would both tighten these intervals and test whether the model's
  restraint on hard/risk declines holds outside this subsample's skew (see
  Phase 4's representativeness table).

## ✅ Phase 6 — Audit trail (execution deliberately out of scope)

**Scope, stated once here and in `README.md` so it cannot be missed:** this
phase does not execute anything against a real payment gateway. There is no
HTTP client anywhere in this codebase, no fake Razorpay server, and none is
planned - wiring an authorised plan to a real API call is explicitly out of
scope for this project. What this phase gives instead is the audit trail an
execution layer would need before anyone could trust it: a durable,
line-by-line record of WHY a rupee was or was not spent, readable without
opening the code.

### `src/retry_economist/audit/ledger.py`

An append-only JSONL ledger, one record per `EconomistDecision`, written to
`results/audit_ledger.jsonl`. "Append-only" is enforced structurally
(`AuditLedger.append` opens the file in `"a"` mode and writes exactly one
line - it never reads its own history back to decide anything) and checked
directly: `test_append_only_never_rewrites_earlier_bytes` writes once, snapshots
the bytes, writes again, and asserts the second run's file starts with the
first run's bytes unchanged.

**Every record carries full decision provenance** - `txn_id`, `decided_at`,
`policy`, `estimator`, `provider` (label + pinned model, or
`"deterministic (no LLM)"` / `null` when the plan source is rule-based), the
three signals with their confidences, the proposal (plan, rationale, and -
when the plan source is LLM-backed - the model's own self-reported
`root_cause`/`root_cause_confidence`/`p_recover_if_act`/`p_recover_if_abstain`),
every compliance rule checked and which fired, the itemised EV terms (every
line of `EV(plan) = amount x delta_p x discount - costs`, not just the net),
the verdict, the final authorised plan, and a human-readable reason.

**Idempotency**: `idempotency_key(txn_id, action, attempt_index)` -
`sha256` over exactly those three values, mirroring `llm/cache.py::cache_key`'s
pattern. `attempt_index` numbers the action's position in the FINAL
authorised plan (after compliance), not the original proposal - a rule
removing the tail of a ladder must not shift the survivors' keys.
`test_idempotency_keys_are_stable_across_two_separate_runs` builds two
independent policy instances over the same transaction and asserts identical
keys; `test_idempotency_index_reflects_the_final_authorised_plan_not_the_proposal`
checks the position guarantee directly with a capped ladder.

### `--audit` on the eval CLI, off by default

```
python -m retry_economist.eval.cli --split holdout --policies "retry_economist (prior)" --audit
python scripts/subsample_scoreboard.py --audit
```

Both append to the SAME `results/audit_ledger.jsonl` (append-only across
separate invocations too, not just within one process) - confirmed by
running `--audit` off (unchanged line count), then each command exactly
once. Run over both required configurations:

| policy | split | n records | approve | approve_truncated | veto |
| --- | --- | ---: | ---: | ---: | ---: |
| `retry_economist (prior)` | full holdout (749) | 749 | 525 | 0 | 224 |
| `retry_economist (LLM plan)` | Phase 4 SUBSAMPLE (47) | 47 | 26 | 0 | 21 |

**Total: 796 records, one per decision, both counts matching the two
scoreboards' own verdict counts exactly** (`results/holdout_scoreboard.md`
and `results/subsample_scoreboard.md`'s Diagnostic 6). `approve_truncated`
is 0 in both, consistent with every prior measurement in this project - see
Phase 5's naive-plan and LLM-plan verdict counts for why a partial
truncation has yet to occur on any real data this project has scored.

One full record, verbatim, from each (`results/audit_ledger.jsonl`, both for
the same transaction, `pay_00861`, an ACS_TIMEOUT / three-DS-dropoff case -
priced two different ways by two different plan sources against the same
train-only prior):

```json
{
  "policy": "retry_economist (prior)",
  "txn_id": "pay_00861",
  "provider": {"label": "deterministic (no LLM)", "model": null},
  "estimator": "historical_prior_train_only",
  "signals": {
    "root_cause": {"value": "three_ds_dropoff", "confidence": 0.95,
      "summary": "code ACS_TIMEOUT and the gateway text both read as three_ds_dropoff"},
    "issuer_health_now": {"value": "normal", "confidence": 0.3,
      "summary": "SBIN shows 1.1x its baseline failure volume in the 6h window around this attempt (1 failures): failure volume within this issuer's normal range"},
    "liquidity_timing": {"value": 7, "confidence": 0.22,
      "summary": "likely credit around day 7 of the month, about 6 day(s) away; inferred from 1 other failure(s) by this customer"}
  },
  "proposal": {
    "plan": ["nudge_then_retry"],
    "rationale": "R-3DS: nudge_then_retry - cardholder abandoned the OTP page; a reminder recovers intent before re-presenting",
    "root_cause": null, "root_cause_confidence": null,
    "p_recover_if_act": null, "p_recover_if_abstain": null
  },
  "compliance": {"allowed_plan": ["nudge_then_retry"], "is_truncated": false,
    "checks": [ /* C1-C5, none fired */ ]},
  "ev": {
    "plan": ["nudge_then_retry"], "amount_paise": 110792, "value_capture_rate": 1.0,
    "p_recover_if_act": 0.527027, "p_recover_if_abstain": 0.378378, "delta_p": 0.148649,
    "expected_days_to_recovery": 1.0, "daily_discount_rate": 0.02, "discount_factor": 0.980392,
    "gross_value_paise": 16146.16, "action_cost_paise": 260, "annoyance_units": 0.16,
    "annoyance_cost_paise": 15360.0, "net_expected_value_paise": 526.16
  },
  "verdict": "approve", "authorised_plan": ["nudge_then_retry"],
  "reason": "net expected value +526.16 paise for ['nudge_then_retry'] under the historical_prior_train_only estimator",
  "idempotency_keys": [{"action": "nudge_then_retry", "attempt_index": 0,
    "key": "50f6bdb58728eeb101c68f916a14284c0cda6fbbfcda9829c5d4a7339fd6f737"}]
}
```

```json
{
  "policy": "retry_economist (LLM plan)",
  "txn_id": "pay_00861",
  "provider": {"label": "gemini:gemini-3.5-flash-lite (SUBSAMPLE - cache replay only, no network)",
    "model": "gemini-3.5-flash-lite"},
  "estimator": "historical_prior_train_only",
  "signals": { /* identical to above - same transaction, same SignalIndex */ },
  "proposal": {
    "plan": ["retry_now"],
    "rationale": "The failure was caused by a 3DS timeout during authentication as indicated by the root_cause signal. Since this is a soft decline and attempts left allows it, an immediate retry is warranted to capture the user before they abandon the flow entirely.",
    "root_cause": "three_ds_dropoff", "root_cause_confidence": 0.95,
    "p_recover_if_act": 0.65, "p_recover_if_abstain": 0.1
  },
  "compliance": {"allowed_plan": ["retry_now"], "is_truncated": false,
    "checks": [ /* C1-C5, none fired */ ]},
  "ev": {
    "plan": ["retry_now"], "amount_paise": 110792, "value_capture_rate": 1.0,
    "p_recover_if_act": 0.472973, "p_recover_if_abstain": 0.378378, "delta_p": 0.094595,
    "expected_days_to_recovery": 0.02, "daily_discount_rate": 0.02, "discount_factor": 0.999604,
    "gross_value_paise": 10476.17, "action_cost_paise": 200, "annoyance_units": 0.03,
    "annoyance_cost_paise": 2880.0, "net_expected_value_paise": 7396.17
  },
  "verdict": "approve", "authorised_plan": ["retry_now"],
  "reason": "net expected value +7396.17 paise for ['retry_now'] under the historical_prior_train_only estimator",
  "idempotency_keys": [{"action": "retry_now", "attempt_index": 0,
    "key": "1f6d01b2384b6a792ef535e0f51aef8efe3f3d9affcd90b2ae0cb00d0b1b1c40"}]
}
```

Note the real model's own `p_recover_if_act` (0.65) and the historical
prior's priced `p_recover_if_act` (0.472973) for the SAME action on the SAME
transaction disagree by 18 points - a concrete, single-line illustration of
Phase 4's finding that the router's self-reported probabilities are not what
prices the decision. The full record carries both, side by side, specifically
so an auditor does not have to take that finding on faith.

**Tests** (`tests/test_audit.py`, 10 tests, no API key or network anywhere in
the file): exactly one record per decision and every line parses as valid
JSON (`test_exactly_one_record_per_decision_and_every_line_is_valid_json`);
idempotency keys unique within a run across ten multi-action plans
(`test_keys_unique_within_a_run`) and stable across two independent runs;
the append-only guarantee; the full-provenance schema, exercised on a real
veto path (a hard decline strips `naive_retry_3x`'s whole ladder via C2);
the EV block is itemised, not just a total; and a secret-scan test extending
`test_router.py::test_no_secret_is_written_to_the_cache`'s pattern to this
new artefact - `GEMINI_API_KEY` set to a dummy value, ledger written, the
value asserted absent from the file.

## ⬜ Phase 7 — Demo, README, architecture diagram

---

## Reproducing

```
pip install -e .
python -m retry_economist.generator.cli --seed 42 --n 2500 --customers 900
python -m retry_economist.eval.cli --split holdout --policies do_nothing,naive_retry_3x,rules_only,oracle_best
python -m retry_economist.eval.cli --split holdout \
    --policies "do_nothing,naive_retry_3x,rules_only,retry_economist (naive plan),retry_economist (prior),oracle_best"
python -m retry_economist.eval.cli --split holdout \
    --policies "do_nothing,naive_retry_3x,rules_only,retry_economist (prior),oracle_best" \
    --clv-sweep --discount-sweep
python scripts/subsample_scoreboard.py   # Phase 4 + 5 end-to-end close-out: real model, n=47, offline, no key needed
python -m retry_economist.eval.cli --split holdout --policies "retry_economist (prior)" --audit   # Phase 6: 749 ledger records
python scripts/subsample_scoreboard.py --audit   # Phase 6: 47 more ledger records, same file, appended
python -m pytest tests -q
```

Add `--limit N` for a smoke run, `--clv-sweep` for the lifetime-value sensitivity
report, `--discount-sweep` for the daily-discount-rate sensitivity report (only
meaningful with `retry_economist (prior)` in `--policies`, since it re-decides
rather than re-prices - see Phase 5), and `--seeds 42,43,44,45,46` for
cross-seed robustness. Everything above runs offline with no API key.

**`python -m retry_economist.eval.cli --split holdout --policies
...,llm_router_only,...` (full 749-transaction holdout) is NOT offline today**:
only 47 of 749 prompts are cached (see Phase 4), so it will attempt 702 live
network calls against `GEMINI_API_KEY` and run into the same ~0.5
calls/minute capacity limit that stopped the original background run. Use
`scripts/subsample_scoreboard.py` for the reproducible, offline, no-key result
on exactly the 47 transactions that are actually cached.
