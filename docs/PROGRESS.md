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
| 4 — Three-signal router | 🟡 REAL MODEL PINNED, RUN IN PROGRESS — full holdout call is running in the background against a hard free-tier rate limit; partial cache only so far, see note below |
| 5 — Economist layer (approve / veto) | ✅ COMPLETE against the historical-prior estimator (no LLM). LLM-probability pairing waits on Phase 4's background run. |
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

## 🟡 Phase 4 — Three-signal router (real model pinned; full run in progress)

The router **proposes only**. A `Proposal` is a distinct type from `Decision`, the
simulator accepts only a `Decision`, and a test walks the router's syntax tree to
assert it never constructs one. Turning a proposal into something executable
happens in three visible lines inside the ablation policy, nowhere else.

### STATUS UPDATE — 2026-09-04

`GEMINI_API_KEY` is now set. `python -m retry_economist.llm.discover` ran for
real against the live API (not simulated) and pinned **`gemini-3.5-flash-lite`**,
chosen as the cheapest flash-tier model with structured JSON output among 11
candidates the API actually listed (full list, and the rejected candidates,
in `src/retry_economist/llm/model_pin.json`, committed).

A 20-transaction smoke test ran against the real model (not `MockProvider`).
Rationales came back grounded in the specific `failure_code` / `gateway_message`
per transaction rather than boilerplate - e.g. one proposal cited a 2.2x issuer
failure-volume spike by name, another cited the customer's own
`past_success_rate`. `root_cause_confidence` was 0.95 on the three inspected in
detail, which is worth watching once the full reliability table is in.

**The full 749-transaction holdout run is in progress in the background**,
started this session and left running per instruction. The free-tier rate limit
is far tighter than assumed: sustained throughput is **~0.5 calls/minute**
(42→47 cached responses over 11 minutes, confirmed against a raw diagnostic
call with the SDK's own internal retry disabled - see the rate-limit note at
the end of this section), which puts the full run at roughly **24 hours**, not
the ~90 minutes originally planned. As of this update, **57 of 749**
transactions have a real cached response. The run has not been killed and will
keep filling the cache; every completed call is retained (see the
resumability test, `tests/test_router.py::test_a_rate_limited_run_is_resumable_without_losing_completed_calls`),
so nothing already answered will be re-fetched.

**Because of this, `results/holdout_scoreboard.md` currently holds Phase 5's
non-LLM main result (see below), not an LLM-router run** - the two experiments
cannot share that file at the same moment, and re-running any LLM policy while
the background job is mid-write would corrupt its cache reads. The stand-in
tables immediately below are **preserved from the earlier mock run for
reference only**; they describe a file that no longer exists in this state.
Real full-holdout LLM numbers - the five-policy board, calibration, reliability,
and five full proposals including at least one judged wrong - will replace this
entire section once the background run finishes or is scored on whatever subset
has cached by morning, with every other policy re-scored on that same subset
for a fair comparison.

### 🛑 NO NUMBER BELOW THIS POINT IN THE SECTION IS AN LLM RESULT

The tables below are unchanged from when `GEMINI_API_KEY` was not reachable and
the router ran on `MockProvider`: a fixed heuristic over the same facts block a
model would receive.

> **Every figure below was produced by that deterministic stand-in. None of it
> is evidence about a language model. Tables are marked `[STAND-IN]`
> individually so a number cannot be quoted out of context.**

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
cache is committed**, so the full evaluation replays offline with no API key:

```
calls=749  network=0  cache_hits=749  hit_rate=100.0%
```

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
**This fix ships in the code but not retroactively in the run already
in progress** — that process loaded the old module before the fix landed and
keeps running with the old classifier, since it was not restarted per
instruction. Tomorrow's scoring step should check the background run's final
`parse_failures` count: any transaction that got silently abstained through
this gap deserves a re-ask (the cache will not have an entry for it under a
fresh, correctly-classified prompt only if the prompt text also changed,
which it did not — so a genuinely gapped transaction would show as a cached
abstain rather than a cache miss, and is only detectable by cross-checking
`parse_failures` against how many proposals came back with an empty plan).

### Degradation is one-directional

Unparseable output gets one repair attempt, then becomes an **ABSTAIN** proposal
and a counter. An action outside the allowed set, or a probability outside [0,1],
invalidates the whole response the same way. A malfunctioning model costs nothing
and can never spend a merchant's money. Three tests cover these paths.

### `[STAND-IN]` Result — the ablation, and two null findings

`[STAND-IN — not an LLM]`

| policy | recovery | uplift pp | acted | attempts | precision | F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `rules_only` | 47.9% | +24.6 | 610 | 568 | 31.0% | 45.2% |
| `llm_router_only (NO ECONOMIST)` *[stand-in]* | 48.2% | +24.8 | 610 | 574 | 31.3% | 45.6% |

Paired, same customers both arms:
`llm_router_only vs rules_only` = **+0.27 pp [-0.26, +0.81]** — straddles zero.

**Null finding 1 — the plans are the same.** The stand-in's playbook coincides with
the rules table, so at plan level this ablation is measuring the rules baseline
twice. It is uninformative by construction, and stated as such rather than dressed
up.

**Null finding 2 — the probability estimates lose to a lookup.** Scored as
forecasts against a per-failure-code prior fitted on the **train split only**
(asserted by test — no holdout customer contributes):

`[STAND-IN — these Brier scores are the heuristic's, not a model's]`

| estimate | n | stand-in Brier | constant Brier | train prior Brier | beats prior? |
| --- | ---: | ---: | ---: | ---: | :---: |
| `p_recover_if_act` | 610 | 0.2637 | 0.2858 | **0.2430** | **no** |
| `p_recover_if_abstain` | 749 | 0.1738 | 0.1791 | **0.1685** | **no** |

The stand-in's estimates beat a constant base rate but **lose to a per-code
historical prior on both**. This is a finding about the heuristic, and it is
**open whether a real model does better** — that is the measurement the harness
is built to make, and it has not been made yet.

Ten-bin reliability tables for both estimates were in `results/holdout_scoreboard.md`
**as of the STAND-IN run**; that file now holds Phase 5's non-LLM main result
instead (see the status update above) - the STAND-IN calibration numbers above
are transcribed here for reference and are not currently reproducible from the
committed `results/` directory in this state.

## ✅ Phase 5 — Economist layer (historical-prior estimator; LLM estimator pending)

Built in `src/retry_economist/economist/`: `EVTerms` (every term of the formula
itemised), five hard compliance rules (`C1`-`C5`, each able only to REMOVE an
action, never add or reorder one), and `Economist.decide()` returning
`approve` / `approve_truncated` / `veto` with a full audit trail. 174 tests
pass, including a property test per compliance rule with a ₹10-crore
transaction confirming EV never overrides one.

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

### Main result — full 749-transaction holdout, no LLM involved

| policy | recovery | uplift pp | decision P / R / F1 | attempts | cost INR | net value INR | INR/INR |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `do_nothing` | 23.4% | +0.0 | n/a / 0.0% / n/a | 0 | 0 | 0 | n/a |
| `naive_retry_3x` | 39.0% | +15.6 | 20.8% / 96.7% / 34.2% | 1,430 | 41,010 | 309,823 | 0.12 |
| `rules_only` | **47.9%** | **+24.6** | 31.0% / 83.6% / 45.2% | 568 | 30,813 | 684,974 | 0.04 |
| `retry_economist (prior)` | 45.4% | +22.0 | 32.4% / 73.0% / 44.9% | **515** | **17,018** | **688,983** | **0.02** |
| `oracle_best` (CHEATS) | 61.4% | +38.1 | 100% / 100% / 100% | 240 | 16,612 | 981,483 | 0.02 |

Paired bootstrap, same customers resampled both arms (2,000 iterations):

| comparison | Δ net uplift pp | Δ decision F1 | supported |
| --- | --- | --- | --- |
| `rules_only` vs `naive_retry_3x` | +8.95 [+5.72, +12.50] | +0.110 [+0.067, +0.154] | yes |
| `rules_only` vs `do_nothing` | +24.57 [+21.61, +27.74] | +0.452 [+0.414, +0.492] | yes |
| `retry_economist (prior)` vs `rules_only` | **−2.54 [−3.63, −1.52]** | −0.004 [−0.022, +0.015] | uplift: yes (economist recovers LESS) · F1: no |
| `retry_economist (prior)` vs `naive_retry_3x` | +6.41 [+3.25, +9.80] | +0.106 [+0.062, +0.154] | yes |

**Stated plainly, because it is the honest result:** the economist recovers
**significantly less** than `rules_only` (−2.54pp, CI excludes zero) while
spending **45% less** (17,018 vs 30,813 INR) and using **9% fewer attempts**
(515 vs 568). It still beats the production-realistic baseline
(`naive_retry_3x`) on both uplift and decision F1. This is EV-gating doing
exactly what it is built to do: decline some actions that would have worked
because, priced against the historical prior's probabilities, they were not
worth the expected annoyance/attempt cost. Whether that trade is "worth it"
depends on the CLV assumption - see the sweep below - not on a single number.

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

### Veto precision - what did vetoing actually cost?

Of `rules_only`'s 610 non-empty proposals, the economist fully approved 525,
truncated none, and fully vetoed 85 (139 more transactions had no proposal to
begin with - `rules_only` itself abstained). Full breakdown in
`results/veto_precision.md`.

| split | n vetoed actions | would have failed anyway | veto precision |
| --- | ---: | ---: | ---: |
| all vetoes | 85 | 43 | 50.6% |
| compliance-driven (`C5_CONTACT_CAP`) | 30 | 14 | 46.7% |
| economics-driven (`EV<=0`) | 55 | 29 | 52.7% |

**Only `C5_CONTACT_CAP` fired on real holdout data** - `C1`, `C2`, `C3` and `C4`
never removed a single action. Not a bug: `rules_only` already abstains on
every hard decline, risk decline and expired mandate itself (see its `RULES`
table and message fallback), and it already respects the attempt cap before
proposing anything, so those four rules never see a live plan to veto under
THIS pairing. `results/veto_demo.txt` demonstrates `C1` directly on a
constructed transaction instead (see below), stating plainly that it is
constructed and why a real example does not exist under this pairing.

**Veto precision just above 50% is the honest headline of this sub-result**:
roughly half of what the economist vetoed on economic or contact-cap grounds
would, per the oracle, have recovered the payment. That is the real price of
the caution behind the −2.54pp uplift gap above - not a free lunch.

### The RISK_DECLINED veto demo

`results/veto_demo.txt` is a full trace - real calls to `compute_ev`,
`apply_compliance` and `Economist.decide`, nothing hand-computed - on a
CONSTRUCTED transaction (stated as such in the file itself) built to isolate
`C1`, primed with a deliberately optimistic estimator (85% assumed success):
hypothetical EV **+INR 18,711.78**, and the actual decision is still
`veto`, plan `()`, because `C1_RISK_DECLINED` removed the only proposed
action before any EV arithmetic ran. Same guarantee
`tests/test_economist.py::test_c1_vetoes_a_risk_decline_at_any_expected_value`
asserts, shown at a realistic transaction size with every intermediate number
visible.

### What's still open

- The LLM-priced pairing (router's plan, either estimator) waits on Phase 4's
  background run - see the status update in that section.
- `approve_truncated` never fired on this holdout: `rules_only` proposes at
  most one action per transaction, so compliance filtering a plan either
  empties it (→ `veto`) or leaves it whole (→ `approve`). The truncation path
  is exercised by unit tests (`test_approve_truncated_when_compliance_removes_something_but_the_rest_is_profitable`)
  and will show up for real once a multi-action plan source (the LLM) is wired in.

## ⬜ Phase 6 — Bounded execution + audit trail

## ⬜ Phase 7 — Demo, README, architecture diagram

---

## Reproducing

```
pip install -e .
python -m retry_economist.generator.cli --seed 42 --n 2500 --customers 900
python -m retry_economist.eval.cli --split holdout --policies do_nothing,naive_retry_3x,rules_only,oracle_best
python -m retry_economist.eval.cli --split holdout \
    --policies "do_nothing,naive_retry_3x,rules_only,retry_economist (prior),oracle_best" \
    --clv-sweep --discount-sweep
python -m retry_economist.eval.cli --split holdout \
    --policies do_nothing,naive_retry_3x,rules_only,llm_router_only,oracle_best
python -m pytest tests -q
```

Add `--limit N` for a smoke run, `--clv-sweep` for the lifetime-value sensitivity
report, `--discount-sweep` for the daily-discount-rate sensitivity report (only
meaningful with `retry_economist (prior)` in `--policies`, since it re-decides
rather than re-prices - see Phase 5), and `--seeds 42,43,44,45,46` for
cross-seed robustness. Everything above runs offline with no API key, except
the `llm_router_only` run, which needs `GEMINI_API_KEY` on a cold cache and
replays offline once `data/llm_cache/` is populated.
