# Retry Economist - scoreboard (holdout_subsample)

> **PHASE 4 CLOSED AS A LABELLED SUBSAMPLE, n=47 of the full 749-transaction holdout.** The background run never finished; see `docs/PROGRESS.md` for why and for the exact selection rule.


- split: **holdout_subsample**
- transactions: **47**
- customers: **41**
- data seed: `42`
- generator hash: `f132a5046aea`
- bootstrap: 2000 clustered iterations (resampling customers), seed `20260601`
- LLM provider: **gemini:gemini-3.5-flash-lite (SUBSAMPLE - cache replay only, no network)**
- recoverable ceiling: **61.7%** within debit-attempt caps, 61.7% ignoring them (scheme caps cost 0.0 pp of recovery outright)

## HEADLINE

> Recovered 53.2% vs 40.4% for naive_retry_3x, using 62% fewer retry attempts.

## Results

| policy | status | decision precision | decision recall | decision F1 | addressable capture | action selection error | precision (INR-wt) | recall (INR-wt) | F1 (INR-wt) | recovery rate (95% CI) | net uplift pp (95% CI) | recovery (INR-wt) | uplift pp (INR-wt) | median days | mean days | recovered <=72h | action rate | abstained | restraint precision | net INR | cost INR | net value INR | INR spent per INR earned | attempts | contact | viol |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `do_nothing` | ok | n/a | 0.0% | n/a | 0.0% | n/a | n/a | 0.0% | n/a | 23.4% [12.8, 35.4] | +0.0 [+0.0, +0.0] | 9.2% | +0.0 | 0.68 | 1.06 | 23.4% | 0.0% | 47 | 61.7% | 0 | 0 | 0 | n/a (no net revenue) | 0 | 0.0% | 0 |
| `naive_retry_3x` | ok | 20.5% | 100.0% | 34.0% | 50.0% | 50.0% | 5.0% | 100.0% | 9.5% | 40.4% [27.3, 54.3] | +17.0 [+4.3, +30.8] | 13.6% | +4.4 | 0.02 | 0.07 | 40.4% | 93.6% | 3 | 100.0% | 9,795 | 2,466 | 7,329 | 0.25 | 86 | 0.0% | 0 |
| `rules_only` | ok | 37.8% | 87.5% | 52.8% | 77.8% | 12.5% | 63.4% | 99.6% | 77.5% | 53.2% [40.7, 65.9] | +29.8 [+17.3, +43.5] | 49.5% | +40.3 | 1.37 | 5.22 | 31.9% | 78.7% | 10 | 80.0% | 89,528 | 2,172 | 87,356 | 0.02 | 33 | 19.1% | 0 |
| `retry_economist (prior)` | ok | 33.3% | 62.5% | 43.5% | 55.6% | 16.7% | 63.5% | 97.6% | 77.0% | 44.7% [32.0, 57.1] | +21.3 [+10.4, +33.3] | 48.7% | +39.5 | 1.02 | 4.79 | 25.5% | 63.8% | 17 | 64.7% | 87,734 | 963 | 86,771 | 0.01 | 30 | 6.4% | 0 |
| `retry_economist (naive plan)` | ok | 41.2% | 43.8% | 42.4% | 38.9% | 22.2% | 6.8% | 71.4% | 12.4% | 38.3% [26.2, 51.1] | +14.9 [+6.0, +25.5] | 13.3% | +4.1 | 0.05 | 0.58 | 38.3% | 36.2% | 30 | 70.0% | 9,059 | 845 | 8,214 | 0.09 | 29 | 0.0% | 0 |
| `llm_router_only (NO ECONOMIST)` | ok | 35.3% | 75.0% | 48.0% | 66.7% | 14.3% | 63.0% | 99.2% | 77.0% | 48.9% [36.2, 61.2] | +25.5 [+13.5, +38.6] | 49.5% | +40.3 | 1.01 | 2.99 | 36.2% | 72.3% | 13 | 69.2% | 89,640 | 2,264 | 87,376 | 0.03 | 29 | 19.1% | 0 |

## Reference bounds - NOT RESULTS

These read the counterfactual outcomes to pick an action already known to work.
No deployable policy can do this; they are here to show how much was ever
available to win.

| policy | status | decision precision | decision recall | decision F1 | addressable capture | action selection error | precision (INR-wt) | recall (INR-wt) | F1 (INR-wt) | recovery rate (95% CI) | net uplift pp (95% CI) | recovery (INR-wt) | uplift pp (INR-wt) | median days | mean days | recovered <=72h | action rate | abstained | restraint precision | net INR | cost INR | net value INR | INR spent per INR earned | attempts | contact | viol |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `oracle_best (CHEATS)` | ok | 100.0% | 100.0% | 100.0% | 100.0% | 0.0% | 100.0% | 100.0% | 100.0% | 61.7% [49.0, 74.5] | +38.3 [+23.9, +53.3] | 50.4% | +41.3 | 0.68 | 1.49 | 53.2% | 38.3% | 29 | 100.0% | 91,671 | 1,406 | 90,265 | 0.02 | 13 | 12.8% | 0 |

## Attribution

Split first on whether the policy acted at all. Restraint is not failure: leaving a customer alone who pays unaided, or one no available action could have recovered, is the system working - at zero cost. **Restraint precision** is the share of untouched transactions that fall in those two buckets.

`futile` is split by whose mistake it was. **hopeless** means no affordable action would ever have recovered it, so the spend should not have been authorised at all - an economics failure. **wrong action** means the opportunity was real and the wrong action was chosen - a routing failure. `action selection error` is the second as a share of the transactions the policy was right to act on.

| policy | acted | incremental | cannibalised | wasted | futile (hopeless) | futile (wrong action) | abstained | correct restraint | correct walkaway | missed opportunity | sum |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `do_nothing` | 0 | 0 | 0 | 0 | 0 | 0 | 47 | 11 | 18 | 18 | 47/47 |
| `naive_retry_3x` | 44 | 9 | 1 | 10 | 15 | 9 | 3 | 0 | 3 | 0 | 47/47 |
| `rules_only` | 37 | 14 | 0 | 11 | 10 | 2 | 10 | 0 | 8 | 2 | 47/47 |
| `retry_economist (prior)` | 30 | 10 | 0 | 10 | 8 | 2 | 17 | 1 | 10 | 6 | 47/47 |
| `retry_economist (naive plan)` | 17 | 7 | 0 | 4 | 4 | 2 | 30 | 7 | 14 | 9 | 47/47 |
| `oracle_best (CHEATS)` | 18 | 18 | 0 | 0 | 0 | 0 | 29 | 11 | 18 | 0 | 47/47 |
| `llm_router_only (NO ECONOMIST)` | 34 | 12 | 0 | 9 | 11 | 2 | 13 | 2 | 7 | 4 | 47/47 |

### Weighted by rupees at risk

The same seven buckets as a share of the money, not of the invoice count. Amounts here span a median around INR 700 to a p95 above INR 31,000, so the two views can rank policies differently - and where they diverge, the divergence is the finding, not a rounding artefact.

| policy | at risk INR | incremental | cannibalised | wasted | futile | correct restraint | correct walkaway | missed opportunity | sum |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `do_nothing` | 222,179 | 0.0% | 0.0% | 0.0% | 0.0% | 9.2% | 49.6% | 41.3% | 100.0% |
| `naive_retry_3x` | 222,179 | 4.5% | 0.1% | 9.1% | 76.7% | 0.0% | 9.6% | 0.0% | 100.0% |
| `rules_only` | 222,179 | 40.3% | 0.0% | 9.2% | 14.1% | 0.0% | 36.3% | 0.2% | 100.0% |
| `retry_economist (prior)` | 222,179 | 39.5% | 0.0% | 9.0% | 13.6% | 0.1% | 36.7% | 1.0% | 100.0% |
| `retry_economist (naive plan)` | 222,179 | 4.1% | 0.0% | 8.2% | 47.7% | 1.0% | 37.4% | 1.6% | 100.0% |
| `oracle_best (CHEATS)` | 222,179 | 41.3% | 0.0% | 0.0% | 0.0% | 9.2% | 49.6% | 0.0% | 100.0% |
| `llm_router_only (NO ECONOMIST)` | 222,179 | 40.3% | 0.0% | 8.8% | 14.9% | 0.4% | 35.2% | 0.3% | 100.0% |

## Paired comparisons

Difference between two policies, bootstrapped over the SAME resampled customers on both sides. An interval excluding zero is a difference the evidence supports.

| comparison | net uplift pp delta (95% CI) | supported | decision F1 delta (95% CI) | supported |
| --- | ---: | :---: | ---: | :---: |
| `rules_only` vs `naive_retry_3x` | +12.77 [+0.00, +26.19] | no | +0.1887 [+0.0301, +0.3691] | yes |
| `rules_only` vs `do_nothing` | +29.79 [+17.31, +43.48] | yes | +0.5283 [+0.3529, +0.6780] | yes |
| `llm_router_only (NO ECONOMIST)` vs `rules_only` | -4.26 [-13.33, +4.17] | no | -0.0483 [-0.1744, +0.0702] | no |
| `llm_router_only (NO ECONOMIST)` vs `naive_retry_3x` | +8.51 [-4.35, +21.74] | no | +0.1404 [-0.0359, +0.3205] | no |
| `retry_economist (prior)` vs `rules_only` | -8.51 [-17.02, -2.04] | yes | -0.0935 [-0.2258, +0.0152] | no |
| `retry_economist (prior)` vs `naive_retry_3x` | +4.26 [-6.52, +16.28] | no | +0.0952 [-0.0690, +0.2718] | no |
| `retry_economist (naive plan)` vs `naive_retry_3x` | -2.13 [-8.93, +4.55] | no | +0.0846 [-0.0407, +0.2186] | no |

## Breakdown by failure code

#### `do_nothing` by failure code

| failure code | n | precision | recall | F1 | addressable | capture | selection error | precision (INR-wt) | recall (INR-wt) | F1 (INR-wt) | recovery | organic | uplift pp | recovery (INR-wt) | uplift pp (INR-wt) | median days | mean days | recovered <=72h | incr | cannib | hopeless | wrong action | missed | restraint precision | net INR | attempts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `51` | 14 | n/a | 0.0% | n/a | 7 | 0.0% | n/a | n/a | 0.0% | n/a | 35.7% | 35.7% | +0.0 | 2.0% | +0.0 | 1.88 | 1.41 | 35.7% | 0 | 0 | 0 | 0 | 7 | 50.0% | 0 | 0 |
| `91` | 13 | n/a | 0.0% | n/a | 5 | 0.0% | n/a | n/a | 0.0% | n/a | 23.1% | 23.1% | +0.0 | 34.6% | +0.0 | 0.29 | 0.41 | 23.1% | 0 | 0 | 0 | 0 | 5 | 61.5% | 0 | 0 |
| `41` | 7 | n/a | 0.0% | n/a | 2 | 0.0% | n/a | n/a | 0.0% | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 0 | 0 | 2 | 71.4% | 0 | 0 |
| `MANDATE_EXPIRED_M06` | 4 | n/a | 0.0% | n/a | 3 | 0.0% | n/a | n/a | 0.0% | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 0 | 0 | 3 | 25.0% | 0 | 0 |
| `U69` | 3 | n/a | 0.0% | n/a | 1 | 0.0% | n/a | n/a | 0.0% | n/a | 33.3% | 33.3% | +0.0 | 15.4% | +0.0 | 1.87 | 1.87 | 33.3% | 0 | 0 | 0 | 0 | 1 | 66.7% | 0 | 0 |
| `96` | 2 | n/a | n/a | n/a | 0 | n/a | n/a | n/a | n/a | n/a | 50.0% | 50.0% | +0.0 | 74.3% | +0.0 | 0.44 | 0.44 | 50.0% | 0 | 0 | 0 | 0 | 0 | 100.0% | 0 | 0 |
| `ACS_TIMEOUT` | 2 | n/a | n/a | n/a | 0 | n/a | n/a | n/a | n/a | n/a | 50.0% | 50.0% | +0.0 | 68.9% | +0.0 | 1.09 | 1.09 | 50.0% | 0 | 0 | 0 | 0 | 0 | 100.0% | 0 | 0 |
| `R05` | 2 | n/a | n/a | n/a | 0 | n/a | n/a | n/a | n/a | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 0 | 0 | 0 | 100.0% | 0 | 0 |

#### `naive_retry_3x` by failure code

| failure code | n | precision | recall | F1 | addressable | capture | selection error | precision (INR-wt) | recall (INR-wt) | F1 (INR-wt) | recovery | organic | uplift pp | recovery (INR-wt) | uplift pp (INR-wt) | median days | mean days | recovered <=72h | incr | cannib | hopeless | wrong action | missed | restraint precision | net INR | attempts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `51` | 14 | 28.6% | 100.0% | 44.4% | 7 | 57.1% | 42.9% | 4.1% | 100.0% | 7.8% | 57.1% | 35.7% | +21.4 | 5.9% | +3.9 | 0.02 | 0.02 | 57.1% | 4 | 1 | 2 | 3 | 0 | n/a | 3,418 | 26 |
| `91` | 13 | 38.5% | 100.0% | 55.6% | 5 | 100.0% | 0.0% | 14.0% | 100.0% | 24.6% | 61.5% | 23.1% | +38.5 | 48.7% | +14.0 | 0.01 | 0.15 | 61.5% | 5 | 0 | 5 | 0 | 0 | n/a | 6,377 | 23 |
| `41` | 7 | 0.0% | n/a | n/a | 2 | 0.0% | 100.0% | 0.0% | n/a | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 3 | 2 | 0 | 100.0% | 0 | 11 |
| `MANDATE_EXPIRED_M06` | 4 | 0.0% | n/a | n/a | 3 | 0.0% | 100.0% | 0.0% | n/a | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 1 | 3 | 0 | n/a | 0 | 9 |
| `U69` | 3 | 0.0% | n/a | n/a | 1 | 0.0% | 100.0% | 0.0% | n/a | n/a | 33.3% | 33.3% | +0.0 | 15.4% | +0.0 | 0.02 | 0.02 | 33.3% | 0 | 0 | 1 | 1 | 0 | n/a | 0 | 7 |
| `96` | 2 | 0.0% | n/a | n/a | 0 | n/a | n/a | 0.0% | n/a | n/a | 50.0% | 50.0% | +0.0 | 74.3% | +0.0 | 0.01 | 0.01 | 50.0% | 0 | 0 | 0 | 0 | 0 | 100.0% | 0 | 1 |
| `ACS_TIMEOUT` | 2 | 0.0% | n/a | n/a | 0 | n/a | n/a | 0.0% | n/a | n/a | 50.0% | 50.0% | +0.0 | 68.9% | +0.0 | 0.01 | 0.01 | 50.0% | 0 | 0 | 1 | 0 | 0 | n/a | 0 | 4 |
| `R05` | 2 | 0.0% | n/a | n/a | 0 | n/a | n/a | 0.0% | n/a | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 2 | 0 | 0 | n/a | 0 | 5 |

#### `rules_only` by failure code

| failure code | n | precision | recall | F1 | addressable | capture | selection error | precision (INR-wt) | recall (INR-wt) | F1 (INR-wt) | recovery | organic | uplift pp | recovery (INR-wt) | uplift pp (INR-wt) | median days | mean days | recovered <=72h | incr | cannib | hopeless | wrong action | missed | restraint precision | net INR | attempts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `51` | 14 | 35.7% | 100.0% | 52.6% | 7 | 71.4% | 28.6% | 91.5% | 100.0% | 95.6% | 71.4% | 35.7% | +35.7 | 93.6% | +91.5 | 6.00 | 11.61 | 0.0% | 5 | 0 | 2 | 2 | 0 | n/a | 81,057 | 14 |
| `91` | 13 | 38.5% | 100.0% | 55.6% | 5 | 100.0% | 0.0% | 14.0% | 100.0% | 24.6% | 61.5% | 23.1% | +38.5 | 48.7% | +14.0 | 1.01 | 1.01 | 61.5% | 5 | 0 | 5 | 0 | 0 | n/a | 6,377 | 13 |
| `41` | 7 | n/a | 0.0% | n/a | 2 | 0.0% | n/a | n/a | 0.0% | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 0 | 0 | 2 | 71.4% | 0 | 0 |
| `MANDATE_EXPIRED_M06` | 4 | 75.0% | 100.0% | 85.7% | 3 | 100.0% | 0.0% | 76.9% | 100.0% | 86.9% | 75.0% | 0.0% | +75.0 | 76.9% | +76.9 | 1.49 | 1.90 | 75.0% | 3 | 0 | 1 | 0 | 0 | n/a | 1,694 | 0 |
| `U69` | 3 | 33.3% | 100.0% | 50.0% | 1 | 100.0% | 0.0% | 20.6% | 100.0% | 34.1% | 66.7% | 33.3% | +33.3 | 36.0% | +20.6 | 0.11 | 0.11 | 66.7% | 1 | 0 | 1 | 0 | 0 | n/a | 399 | 3 |
| `96` | 2 | 0.0% | n/a | n/a | 0 | n/a | n/a | 0.0% | n/a | n/a | 50.0% | 50.0% | +0.0 | 74.3% | +0.0 | 0.09 | 0.09 | 50.0% | 0 | 0 | 0 | 0 | 0 | 100.0% | 0 | 1 |
| `ACS_TIMEOUT` | 2 | 0.0% | n/a | n/a | 0 | n/a | n/a | 0.0% | n/a | n/a | 50.0% | 50.0% | +0.0 | 68.9% | +0.0 | 0.36 | 0.36 | 50.0% | 0 | 0 | 1 | 0 | 0 | n/a | 0 | 2 |
| `R05` | 2 | n/a | n/a | n/a | 0 | n/a | n/a | n/a | n/a | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 0 | 0 | 0 | 100.0% | 0 | 0 |

#### `retry_economist (prior)` by failure code

| failure code | n | precision | recall | F1 | addressable | capture | selection error | precision (INR-wt) | recall (INR-wt) | F1 (INR-wt) | recovery | organic | uplift pp | recovery (INR-wt) | uplift pp (INR-wt) | median days | mean days | recovered <=72h | incr | cannib | hopeless | wrong action | missed | restraint precision | net INR | attempts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `51` | 14 | 30.8% | 80.0% | 44.4% | 7 | 57.1% | 33.3% | 91.5% | 99.9% | 95.5% | 64.3% | 35.7% | +28.6 | 93.4% | +91.4 | 5.84 | 10.02 | 0.0% | 4 | 0 | 2 | 2 | 1 | 0.0% | 80,958 | 13 |
| `91` | 13 | 38.5% | 100.0% | 55.6% | 5 | 100.0% | 0.0% | 14.0% | 100.0% | 24.6% | 61.5% | 23.1% | +38.5 | 48.7% | +14.0 | 1.01 | 1.01 | 61.5% | 5 | 0 | 5 | 0 | 0 | n/a | 6,377 | 13 |
| `41` | 7 | n/a | 0.0% | n/a | 2 | 0.0% | n/a | n/a | 0.0% | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 0 | 0 | 2 | 71.4% | 0 | 0 |
| `MANDATE_EXPIRED_M06` | 4 | n/a | 0.0% | n/a | 3 | 0.0% | n/a | n/a | 0.0% | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 0 | 0 | 3 | 25.0% | 0 | 0 |
| `U69` | 3 | 50.0% | 100.0% | 66.7% | 1 | 100.0% | 0.0% | 24.3% | 100.0% | 39.1% | 66.7% | 33.3% | +33.3 | 36.0% | +20.6 | 0.98 | 0.98 | 66.7% | 1 | 0 | 1 | 0 | 0 | 100.0% | 399 | 2 |
| `96` | 2 | 0.0% | n/a | n/a | 0 | n/a | n/a | 0.0% | n/a | n/a | 50.0% | 50.0% | +0.0 | 74.3% | +0.0 | 0.09 | 0.09 | 50.0% | 0 | 0 | 0 | 0 | 0 | 100.0% | 0 | 1 |
| `ACS_TIMEOUT` | 2 | 0.0% | n/a | n/a | 0 | n/a | n/a | 0.0% | n/a | n/a | 50.0% | 50.0% | +0.0 | 68.9% | +0.0 | 0.36 | 0.36 | 50.0% | 0 | 0 | 0 | 0 | 0 | 100.0% | 0 | 1 |
| `R05` | 2 | n/a | n/a | n/a | 0 | n/a | n/a | n/a | n/a | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 0 | 0 | 0 | 100.0% | 0 | 0 |

#### `retry_economist (naive plan)` by failure code

| failure code | n | precision | recall | F1 | addressable | capture | selection error | precision (INR-wt) | recall (INR-wt) | F1 (INR-wt) | recovery | organic | uplift pp | recovery (INR-wt) | uplift pp (INR-wt) | median days | mean days | recovered <=72h | incr | cannib | hopeless | wrong action | missed | restraint precision | net INR | attempts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `51` | 14 | 40.0% | 40.0% | 40.0% | 7 | 28.6% | 50.0% | 3.2% | 69.4% | 6.1% | 50.0% | 35.7% | +14.3 | 5.1% | +3.0 | 0.38 | 1.01 | 50.0% | 2 | 0 | 1 | 2 | 3 | 66.7% | 2,682 | 11 |
| `91` | 13 | 55.6% | 100.0% | 71.4% | 5 | 100.0% | 0.0% | 14.2% | 100.0% | 24.9% | 61.5% | 23.1% | +38.5 | 48.7% | +14.0 | 0.01 | 0.18 | 61.5% | 5 | 0 | 2 | 0 | 0 | 100.0% | 6,377 | 13 |
| `41` | 7 | n/a | 0.0% | n/a | 2 | 0.0% | n/a | n/a | 0.0% | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 0 | 0 | 2 | 71.4% | 0 | 0 |
| `MANDATE_EXPIRED_M06` | 4 | n/a | 0.0% | n/a | 3 | 0.0% | n/a | n/a | 0.0% | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 0 | 0 | 3 | 25.0% | 0 | 0 |
| `U69` | 3 | 0.0% | 0.0% | n/a | 1 | 0.0% | n/a | 0.0% | 0.0% | n/a | 33.3% | 33.3% | +0.0 | 15.4% | +0.0 | 1.87 | 1.87 | 33.3% | 0 | 0 | 1 | 0 | 1 | 50.0% | 0 | 3 |
| `96` | 2 | 0.0% | n/a | n/a | 0 | n/a | n/a | 0.0% | n/a | n/a | 50.0% | 50.0% | +0.0 | 74.3% | +0.0 | 0.01 | 0.01 | 50.0% | 0 | 0 | 0 | 0 | 0 | 100.0% | 0 | 1 |
| `ACS_TIMEOUT` | 2 | 0.0% | n/a | n/a | 0 | n/a | n/a | 0.0% | n/a | n/a | 50.0% | 50.0% | +0.0 | 68.9% | +0.0 | 0.01 | 0.01 | 50.0% | 0 | 0 | 0 | 0 | 0 | 100.0% | 0 | 1 |
| `R05` | 2 | n/a | n/a | n/a | 0 | n/a | n/a | n/a | n/a | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 0 | 0 | 0 | 100.0% | 0 | 0 |

#### `oracle_best (CHEATS)` by failure code

| failure code | n | precision | recall | F1 | addressable | capture | selection error | precision (INR-wt) | recall (INR-wt) | F1 (INR-wt) | recovery | organic | uplift pp | recovery (INR-wt) | uplift pp (INR-wt) | median days | mean days | recovered <=72h | incr | cannib | hopeless | wrong action | missed | restraint precision | net INR | attempts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `51` | 14 | 100.0% | 100.0% | 100.0% | 7 | 100.0% | 0.0% | 100.0% | 100.0% | 100.0% | 85.7% | 35.7% | +50.0 | 95.6% | +93.6 | 1.13 | 2.18 | 64.3% | 7 | 0 | 0 | 0 | 0 | 100.0% | 82,852 | 7 |
| `91` | 13 | 100.0% | 100.0% | 100.0% | 5 | 100.0% | 0.0% | 100.0% | 100.0% | 100.0% | 61.5% | 23.1% | +38.5 | 48.7% | +14.0 | 0.17 | 0.29 | 61.5% | 5 | 0 | 0 | 0 | 0 | 100.0% | 6,377 | 5 |
| `41` | 7 | 100.0% | 100.0% | 100.0% | 2 | 100.0% | 0.0% | 100.0% | 100.0% | 100.0% | 28.6% | 0.0% | +28.6 | 1.5% | +1.5 | 2.71 | 2.71 | 14.3% | 2 | 0 | 0 | 0 | 0 | 100.0% | 348 | 0 |
| `MANDATE_EXPIRED_M06` | 4 | 100.0% | 100.0% | 100.0% | 3 | 100.0% | 0.0% | 100.0% | 100.0% | 100.0% | 75.0% | 0.0% | +75.0 | 76.9% | +76.9 | 1.49 | 1.90 | 75.0% | 3 | 0 | 0 | 0 | 0 | 100.0% | 1,694 | 0 |
| `U69` | 3 | 100.0% | 100.0% | 100.0% | 1 | 100.0% | 0.0% | 100.0% | 100.0% | 100.0% | 66.7% | 33.3% | +33.3 | 36.0% | +20.6 | 0.98 | 0.98 | 66.7% | 1 | 0 | 0 | 0 | 0 | 100.0% | 399 | 1 |
| `96` | 2 | n/a | n/a | n/a | 0 | n/a | n/a | n/a | n/a | n/a | 50.0% | 50.0% | +0.0 | 74.3% | +0.0 | 0.44 | 0.44 | 50.0% | 0 | 0 | 0 | 0 | 0 | 100.0% | 0 | 0 |
| `ACS_TIMEOUT` | 2 | n/a | n/a | n/a | 0 | n/a | n/a | n/a | n/a | n/a | 50.0% | 50.0% | +0.0 | 68.9% | +0.0 | 1.09 | 1.09 | 50.0% | 0 | 0 | 0 | 0 | 0 | 100.0% | 0 | 0 |
| `R05` | 2 | n/a | n/a | n/a | 0 | n/a | n/a | n/a | n/a | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 0 | 0 | 0 | 100.0% | 0 | 0 |

#### `llm_router_only (NO ECONOMIST)` by failure code

| failure code | n | precision | recall | F1 | addressable | capture | selection error | precision (INR-wt) | recall (INR-wt) | F1 (INR-wt) | recovery | organic | uplift pp | recovery (INR-wt) | uplift pp (INR-wt) | median days | mean days | recovered <=72h | incr | cannib | hopeless | wrong action | missed | restraint precision | net INR | attempts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `51` | 14 | 40.0% | 66.7% | 50.0% | 7 | 57.1% | 20.0% | 93.4% | 99.6% | 96.4% | 64.3% | 35.7% | +28.6 | 94.2% | +92.2 | 3.74 | 6.69 | 21.4% | 4 | 0 | 2 | 1 | 2 | 50.0% | 81,668 | 10 |
| `91` | 13 | 30.8% | 100.0% | 47.1% | 5 | 80.0% | 20.0% | 12.9% | 100.0% | 22.9% | 53.8% | 23.1% | +30.8 | 47.6% | +12.9 | 0.09 | 0.35 | 53.8% | 4 | 0 | 5 | 1 | 0 | n/a | 5,878 | 13 |
| `41` | 7 | 0.0% | 0.0% | n/a | 2 | 0.0% | n/a | 0.0% | 0.0% | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 1 | 0 | 2 | 66.7% | 0 | 0 |
| `MANDATE_EXPIRED_M06` | 4 | 75.0% | 100.0% | 85.7% | 3 | 100.0% | 0.0% | 76.9% | 100.0% | 86.9% | 75.0% | 0.0% | +75.0 | 76.9% | +76.9 | 1.49 | 1.90 | 75.0% | 3 | 0 | 1 | 0 | 0 | n/a | 1,694 | 0 |
| `U69` | 3 | 33.3% | 100.0% | 50.0% | 1 | 100.0% | 0.0% | 20.6% | 100.0% | 34.1% | 66.7% | 33.3% | +33.3 | 36.0% | +20.6 | 0.11 | 0.11 | 66.7% | 1 | 0 | 1 | 0 | 0 | n/a | 399 | 3 |
| `96` | 2 | 0.0% | n/a | n/a | 0 | n/a | n/a | 0.0% | n/a | n/a | 50.0% | 50.0% | +0.0 | 74.3% | +0.0 | 0.09 | 0.09 | 50.0% | 0 | 0 | 0 | 0 | 0 | 100.0% | 0 | 1 |
| `ACS_TIMEOUT` | 2 | 0.0% | n/a | n/a | 0 | n/a | n/a | 0.0% | n/a | n/a | 50.0% | 50.0% | +0.0 | 68.9% | +0.0 | 0.01 | 0.01 | 50.0% | 0 | 0 | 1 | 0 | 0 | n/a | 0 | 2 |
| `R05` | 2 | n/a | n/a | n/a | 0 | n/a | n/a | n/a | n/a | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 0 | 0 | 0 | 100.0% | 0 | 0 |

## Router and provider

- provider: `gemini:gemini-3.5-flash-lite (SUBSAMPLE - cache replay only, no network)`
- proposals: 47
- parse failures (degraded to abstain): **0**
- schema violations (degraded to abstain): 0
- abstain proposals: 13
- calls: 47 (0 reached the provider)
- cache hit rate: 100.0% (47 hits / 0 misses)
- mean latency: 0.0001s per call

## Calibration of the router's probability estimates

The plan is replaceable - a lookup table produces good plans. The probabilities are not: the economist layer cannot compute an expected value without them. So they are scored as forecasts, against a constant base rate and against a per-failure-code historical prior **fitted on the train split only** (1751 transactions). Lower Brier is better.

| estimate | n scored | base rate | router Brier | constant Brier | historical prior Brier | beats prior? |
| --- | ---: | ---: | ---: | ---: | ---: | :---: |
| `p_recover_if_act` | 34 | 0.6176 | 0.2780 | 0.2962 | 0.2449 | no |
| `p_recover_if_abstain` | 47 | 0.2340 | 0.2026 | 0.1793 | 0.1587 | no |

> p_recover_if_act: router Brier 0.2780 does NOT beat the per-code historical prior 0.2449 - the estimates add nothing over a lookup on this data
> p_recover_if_abstain: router Brier 0.2026 does NOT beat the per-code historical prior 0.1587 - the estimates add nothing over a lookup on this data

### Reliability - `p_recover_if_act`

| predicted bin | n | mean predicted | observed frequency | gap |
| --- | ---: | ---: | ---: | ---: |
| [0.3, 0.4) | 2 | 0.350 | 0.500 | -0.150 |
| [0.6, 0.7) | 7 | 0.654 | 0.857 | -0.203 |
| [0.7, 0.8) | 16 | 0.744 | 0.625 | +0.119 |
| [0.8, 0.9) | 9 | 0.844 | 0.444 | +0.400 |

### Reliability - `p_recover_if_abstain`

| predicted bin | n | mean predicted | observed frequency | gap |
| --- | ---: | ---: | ---: | ---: |
| [0.0, 0.1) | 15 | 0.018 | 0.067 | -0.049 |
| [0.1, 0.2) | 9 | 0.130 | 0.667 | -0.537 |
| [0.2, 0.3) | 6 | 0.217 | 0.000 | +0.217 |
| [0.3, 0.4) | 5 | 0.320 | 0.400 | -0.080 |
| [0.4, 0.5) | 11 | 0.409 | 0.182 | +0.227 |
| [0.5, 0.6) | 1 | 0.550 | 0.000 | +0.550 |

## Cost assumptions

Every figure below is an estimate; see `eval/costs.py` for the basis of each.

| constant | value |
| --- | ---: |
| `ATTEMPT_COST_PAISE` | 200 |
| `SMS_COST_PAISE` | 20 |
| `WHATSAPP_COST_PAISE` | 30 |
| `PAYMENT_LINK_COST_PAISE` | 30 |
| `NEW_MANDATE_REQUEST_PAISE` | 500 |
| `HUMAN_ESCALATION_PAISE` | 4500 |
| `CUSTOMER_LIFETIME_VALUE_PAISE` | 1200000 |
| `ANNOYANCE_TO_CHURN_PER_UNIT` | 0.08 |
| `ANNOYANCE_PAISE_PER_UNIT_DERIVED` | 96000.0 |
| `VALUE_CAPTURE_RATE` | 1.0 |

_annoyance priced through churn against a lifetime value of INR 12,000 (an ASSUMPTION, not a measurement), giving INR 960 per annoyance unit; run --clv-sweep to see which conclusions survive changing it._

## Subsample definition and representativeness

The SUBSAMPLE is exactly the holdout transactions with a real cached `gemini-3.5-flash-lite` response reachable under today's code with zero network calls (see the module docstring in `scripts/subsample_scoreboard.py` for exactly how that was determined, including why one transaction with a stale-context cache entry was excluded rather than risking a live call).

| failure code | n in SUBSAMPLE | share of SUBSAMPLE | n in full holdout | share of full holdout | skew (pp) |
| --- | ---: | ---: | ---: | ---: | ---: |
| `51` | 14 | 29.8% | 360 | 48.1% | -18.3 |
| `91` | 13 | 27.7% | 129 | 17.2% | +10.4 |
| `41` | 7 | 14.9% | 57 | 7.6% | +7.3 |
| `96` | 2 | 4.3% | 56 | 7.5% | -3.2 |
| `R05` | 2 | 4.3% | 41 | 5.5% | -1.2 |
| `MANDATE_EXPIRED_M06` | 4 | 8.5% | 36 | 4.8% | +3.7 |
| `U69` | 3 | 6.4% | 36 | 4.8% | +1.6 |
| `ACS_TIMEOUT` | 2 | 4.3% | 34 | 4.5% | -0.3 |

## Diagnostic 1 - action distribution: model vs `rules_only` vs `oracle_best`

Counts are of PROPOSED actions (validated, pre-compliance-truncation), summed across all 47 SUBSAMPLE transactions. A policy proposing an ordered plan of more than one action counts every action in it once.

| action | model (llm_router_only) | rules_only | oracle_best (CHEATS) |
| --- | ---: | ---: | ---: |
| `retry_now` | 2 | 0 | 7 |
| `retry_in_2h` | 11 | 1 | 1 |
| `retry_in_24h` | 3 | 13 | 1 |
| `retry_next_salary_day` | 9 | 14 | 3 |
| `nudge_then_retry` | 0 | 2 | 0 |
| `switch_to_upi_intent` | 4 | 3 | 1 |
| `request_new_mandate` | 5 | 4 | 5 |
| `escalate_to_human` | 0 | 0 | 0 |

> `retry_next_salary_day`: the model proposed it **9** time(s) across 47 transactions (`rules_only`: 14, `oracle_best`: 3).
> Actions the model **never** proposed on this subsample, despite `rules_only` and/or `oracle_best` using them: `nudge_then_retry`.

## Diagnostic 2 - `root_cause_confidence` histogram

| bin | count |
| --- | ---: |
| [0.9, 1.0) | 47 |

> **Effectively constant**: 1 distinct value(s) across 47 proposals, range [0.950, 0.950]. This signal is **uninformative** on this subsample - it carries no discriminating information about which proposals to trust more.

## Diagnostic 3 - `p_recover_if_abstain` vs the TRUE organic rate, by failure code

TRUE organic rate is `would_pay_anyway` measured directly from the oracle on this SUBSAMPLE (policy-independent - the same for every policy since it is a property of the transaction, not of any decision).

| failure code | n | model mean p_recover_if_abstain | true organic rate | signed gap (model - true) |
| --- | ---: | ---: | ---: | ---: |
| `41` | 7 | 0.000 | 0.000 | +0.000 |
| `51` | 14 | 0.188 | 0.357 | -0.169 |
| `91` | 13 | 0.377 | 0.231 | +0.146 |
| `96` | 2 | 0.325 | 0.500 | -0.175 |
| `ACS_TIMEOUT` | 2 | 0.150 | 0.500 | -0.350 |
| `MANDATE_EXPIRED_M06` | 4 | 0.028 | 0.000 | +0.028 |
| `R05` | 2 | 0.000 | 0.000 | +0.000 |
| `U69` | 3 | 0.267 | 0.333 | -0.067 |

## Diagnostic 4 - is the router-vs-rules_only comparison powered?

> `llm_router_only (NO ECONOMIST)` vs `rules_only`: net uplift -4.26 pp [-13.33, +4.17] - STRADDLES ZERO - at this sample size (n=47), this comparison is UNDERPOWERED. The point estimate (-4.26 pp) is not a claim either way; the honest read is 'not enough data to tell', not 'no difference' and not 'a win'.

## Diagnostic 5 - does the real model beat the train-only prior?

> `p_recover_if_act` (n=34): router (real model) Brier 0.2780 vs constant base-rate Brier 0.2962 vs train-only historical-prior Brier 0.2449 - **NO, the real model does NOT beat the prior**.
> `p_recover_if_abstain` (n=47): router (real model) Brier 0.2026 vs constant base-rate Brier 0.1793 vs train-only historical-prior Brier 0.1587 - **NO, the real model does NOT beat the prior**.

