# Retry Economist - scoreboard (holdout)

- split: **holdout**
- transactions: **749**
- customers: **240**
- data seed: `42`
- generator hash: `f132a5046aea`
- bootstrap: 2000 clustered iterations (resampling customers), seed `20260601`
- recoverable ceiling: **61.4%** within debit-attempt caps, 61.7% ignoring them (scheme caps cost 0.3 pp of recovery outright)

## HEADLINE

> Recovered 47.9% vs 39.0% for naive_retry_3x, using 60% fewer retry attempts.

## Results

| policy | status | decision precision | decision recall | decision F1 | addressable capture | action selection error | precision (INR-wt) | recall (INR-wt) | F1 (INR-wt) | recovery rate (95% CI) | net uplift pp (95% CI) | recovery (INR-wt) | uplift pp (INR-wt) | median days | mean days | recovered <=72h | action rate | abstained | restraint precision | net INR | cost INR | net value INR | INR spent per INR earned | attempts | contact | viol |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `do_nothing` | ok | n/a | 0.0% | n/a | 0.0% | n/a | n/a | 0.0% | n/a | 23.4% [20.4, 26.3] | +0.0 [+0.0, +0.0] | 20.6% | +0.0 | 1.48 | 1.51 | 23.4% | 0.0% | 749 | 61.9% | 0 | 0 | 0 | n/a (no net revenue) | 0 | 0.0% | 0 |
| `naive_retry_3x` | ok | 20.8% | 96.7% | 34.2% | 51.2% | 47.9% | 11.8% | 95.2% | 21.1% | 39.0% [34.9, 43.0] | +15.6 [+12.2, +18.9] | 30.5% | +9.9 | 0.01 | 0.13 | 39.0% | 93.7% | 47 | 89.4% | 350,833 | 41,010 | 309,823 | 0.12 | 1430 | 0.0% | 0 |
| `rules_only` | ok | 31.0% | 83.6% | 45.2% | 66.3% | 23.8% | 29.4% | 83.0% | 43.4% | 47.9% [44.1, 51.7] | +24.6 [+21.6, +27.7] | 40.8% | +20.2 | 2.09 | 6.04 | 25.9% | 81.4% | 139 | 73.4% | 715,787 | 30,813 | 684,974 | 0.04 | 568 | 14.0% | 0 |
| `retry_economist (naive plan)` | ok | 28.2% | 33.3% | 30.6% | 28.4% | 34.1% | 16.7% | 58.1% | 25.9% | 33.0% [29.3, 36.5] | +9.6 [+7.1, +12.1] | 30.1% | +9.5 | 0.02 | 0.61 | 33.0% | 38.3% | 462 | 64.9% | 336,983 | 14,917 | 322,066 | 0.04 | 513 | 0.0% | 0 |
| `retry_economist (prior)` | ok | 32.4% | 73.0% | 44.9% | 59.6% | 23.4% | 31.2% | 79.2% | 44.8% | 45.4% [41.6, 49.1] | +22.0 [+19.2, +25.1] | 40.5% | +19.9 | 2.13 | 5.98 | 24.8% | 70.1% | 224 | 71.9% | 706,001 | 17,018 | 688,983 | 0.02 | 515 | 5.1% | 0 |

## Reference bounds - NOT RESULTS

These read the counterfactual outcomes to pick an action already known to work.
No deployable policy can do this; they are here to show how much was ever
available to win.

| policy | status | decision precision | decision recall | decision F1 | addressable capture | action selection error | precision (INR-wt) | recall (INR-wt) | F1 (INR-wt) | recovery rate (95% CI) | net uplift pp (95% CI) | recovery (INR-wt) | uplift pp (INR-wt) | median days | mean days | recovered <=72h | action rate | abstained | restraint precision | net INR | cost INR | net value INR | INR spent per INR earned | attempts | contact | viol |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `oracle_best (CHEATS)` | ok | 100.0% | 100.0% | 100.0% | 100.0% | 0.0% | 100.0% | 100.0% | 100.0% | 61.4% [57.4, 65.4] | +38.1 [+34.7, +41.4] | 48.8% | +28.2 | 1.01 | 2.13 | 52.2% | 38.1% | 464 | 100.0% | 998,095 | 16,612 | 981,483 | 0.02 | 240 | 7.9% | 0 |

## Attribution

Split first on whether the policy acted at all. Restraint is not failure: leaving a customer alone who pays unaided, or one no available action could have recovered, is the system working - at zero cost. **Restraint precision** is the share of untouched transactions that fall in those two buckets.

`futile` is split by whose mistake it was. **hopeless** means no affordable action would ever have recovered it, so the spend should not have been authorised at all - an economics failure. **wrong action** means the opportunity was real and the wrong action was chosen - a routing failure. `action selection error` is the second as a share of the transactions the policy was right to act on.

| policy | acted | incremental | cannibalised | wasted | futile (hopeless) | futile (wrong action) | abstained | correct restraint | correct walkaway | missed opportunity | sum |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `do_nothing` | 0 | 0 | 0 | 0 | 0 | 0 | 749 | 175 | 289 | 285 | 749/749 |
| `naive_retry_3x` | 702 | 146 | 29 | 137 | 256 | 134 | 47 | 9 | 33 | 5 | 749/749 |
| `rules_only` | 610 | 189 | 5 | 161 | 196 | 59 | 139 | 9 | 93 | 37 | 749/749 |
| `retry_economist (naive plan)` | 287 | 81 | 9 | 72 | 83 | 42 | 462 | 94 | 206 | 162 | 749/749 |
| `retry_economist (prior)` | 525 | 170 | 5 | 138 | 160 | 52 | 224 | 32 | 129 | 63 | 749/749 |
| `oracle_best (CHEATS)` | 285 | 285 | 0 | 0 | 0 | 0 | 464 | 175 | 289 | 0 | 749/749 |

### Weighted by rupees at risk

The same seven buckets as a share of the money, not of the invoice count. Amounts here span a median around INR 700 to a p95 above INR 31,000, so the two views can rank policies differently - and where they diverge, the divergence is the finding, not a rounding artefact.

| policy | at risk INR | incremental | cannibalised | wasted | futile | correct restraint | correct walkaway | missed opportunity | sum |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `do_nothing` | 3,544,846 | 0.0% | 0.0% | 0.0% | 0.0% | 20.6% | 51.2% | 28.2% | 100.0% |
| `naive_retry_3x` | 3,544,846 | 11.0% | 1.1% | 16.6% | 64.3% | 2.9% | 3.5% | 0.6% | 100.0% |
| `rules_only` | 3,544,846 | 20.9% | 0.7% | 17.0% | 32.5% | 2.9% | 21.7% | 4.3% | 100.0% |
| `retry_economist (naive plan)` | 3,544,846 | 10.4% | 0.9% | 16.1% | 35.2% | 3.6% | 26.2% | 7.5% | 100.0% |
| `retry_economist (prior)` | 3,544,846 | 20.7% | 0.7% | 15.0% | 29.9% | 4.9% | 23.4% | 5.4% | 100.0% |
| `oracle_best (CHEATS)` | 3,544,846 | 28.2% | 0.0% | 0.0% | 0.0% | 20.6% | 51.2% | 0.0% | 100.0% |

## Paired comparisons

Difference between two policies, bootstrapped over the SAME resampled customers on both sides. An interval excluding zero is a difference the evidence supports.

| comparison | net uplift pp delta (95% CI) | supported | decision F1 delta (95% CI) | supported |
| --- | ---: | :---: | ---: | :---: |
| `rules_only` vs `naive_retry_3x` | +8.95 [+5.72, +12.50] | yes | +0.1098 [+0.0672, +0.1542] | yes |
| `rules_only` vs `do_nothing` | +24.57 [+21.61, +27.74] | yes | +0.4522 [+0.4139, +0.4920] | yes |
| `retry_economist (prior)` vs `rules_only` | -2.54 [-3.63, -1.52] | yes | -0.0036 [-0.0222, +0.0145] | no |
| `retry_economist (prior)` vs `naive_retry_3x` | +6.41 [+3.25, +9.80] | yes | +0.1062 [+0.0620, +0.1537] | yes |
| `retry_economist (naive plan)` vs `naive_retry_3x` | -6.01 [-8.53, -3.66] | yes | -0.0367 [-0.0754, +0.0020] | no |

## Breakdown by failure code

#### `do_nothing` by failure code

| failure code | n | precision | recall | F1 | addressable | capture | selection error | precision (INR-wt) | recall (INR-wt) | F1 (INR-wt) | recovery | organic | uplift pp | recovery (INR-wt) | uplift pp (INR-wt) | median days | mean days | recovered <=72h | incr | cannib | hopeless | wrong action | missed | restraint precision | net INR | attempts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `51` | 360 | n/a | 0.0% | n/a | 134 | 0.0% | n/a | n/a | 0.0% | n/a | 28.9% | 28.9% | +0.0 | 20.4% | +0.0 | 1.42 | 1.51 | 28.9% | 0 | 0 | 0 | 0 | 134 | 62.8% | 0 | 0 |
| `91` | 129 | n/a | 0.0% | n/a | 64 | 0.0% | n/a | n/a | 0.0% | n/a | 22.5% | 22.5% | +0.0 | 36.2% | +0.0 | 1.69 | 1.52 | 22.5% | 0 | 0 | 0 | 0 | 64 | 50.4% | 0 | 0 |
| `41` | 57 | n/a | 0.0% | n/a | 22 | 0.0% | n/a | n/a | 0.0% | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 0 | 0 | 22 | 61.4% | 0 | 0 |
| `96` | 56 | n/a | 0.0% | n/a | 25 | 0.0% | n/a | n/a | 0.0% | n/a | 26.8% | 26.8% | +0.0 | 28.8% | +0.0 | 1.38 | 1.35 | 26.8% | 0 | 0 | 0 | 0 | 25 | 55.4% | 0 | 0 |
| `R05` | 41 | n/a | 0.0% | n/a | 10 | 0.0% | n/a | n/a | 0.0% | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 0 | 0 | 10 | 75.6% | 0 | 0 |
| `MANDATE_EXPIRED_M06` | 36 | n/a | 0.0% | n/a | 15 | 0.0% | n/a | n/a | 0.0% | n/a | 8.3% | 8.3% | +0.0 | 0.2% | +0.0 | 2.00 | 1.77 | 8.3% | 0 | 0 | 0 | 0 | 15 | 58.3% | 0 | 0 |
| `U69` | 36 | n/a | 0.0% | n/a | 12 | 0.0% | n/a | n/a | 0.0% | n/a | 27.8% | 27.8% | +0.0 | 25.8% | +0.0 | 1.70 | 1.52 | 27.8% | 0 | 0 | 0 | 0 | 12 | 66.7% | 0 | 0 |
| `ACS_TIMEOUT` | 34 | n/a | 0.0% | n/a | 3 | 0.0% | n/a | n/a | 0.0% | n/a | 41.2% | 41.2% | +0.0 | 66.5% | +0.0 | 1.76 | 1.57 | 41.2% | 0 | 0 | 0 | 0 | 3 | 91.2% | 0 | 0 |

#### `naive_retry_3x` by failure code

| failure code | n | precision | recall | F1 | addressable | capture | selection error | precision (INR-wt) | recall (INR-wt) | F1 (INR-wt) | recovery | organic | uplift pp | recovery (INR-wt) | uplift pp (INR-wt) | median days | mean days | recovered <=72h | incr | cannib | hopeless | wrong action | missed | restraint precision | net INR | attempts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `51` | 360 | 19.8% | 98.5% | 32.9% | 134 | 50.0% | 49.6% | 9.6% | 85.8% | 17.2% | 40.6% | 28.9% | +11.7 | 26.0% | +5.6 | 0.01 | 0.16 | 40.6% | 67 | 25 | 108 | 66 | 1 | 95.2% | 72,386 | 691 |
| `91` | 129 | 40.0% | 94.1% | 56.1% | 64 | 75.0% | 21.3% | 25.2% | 99.0% | 40.1% | 58.9% | 22.5% | +36.4 | 60.8% | +24.6 | 0.02 | 0.11 | 58.9% | 48 | 1 | 30 | 13 | 3 | 66.7% | 124,956 | 218 |
| `41` | 57 | 0.0% | n/a | n/a | 22 | 0.0% | 100.0% | 0.0% | n/a | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 32 | 22 | 0 | 100.0% | 0 | 144 |
| `96` | 56 | 46.2% | 100.0% | 63.2% | 25 | 96.0% | 4.0% | 35.2% | 100.0% | 52.0% | 69.6% | 26.8% | +42.9 | 63.7% | +34.9 | 0.01 | 0.03 | 69.6% | 24 | 0 | 13 | 1 | 0 | 100.0% | 127,977 | 76 |
| `R05` | 41 | 0.0% | n/a | n/a | 10 | 0.0% | 100.0% | 0.0% | n/a | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 29 | 10 | 0 | 100.0% | 0 | 101 |
| `MANDATE_EXPIRED_M06` | 36 | 0.0% | n/a | n/a | 15 | 0.0% | 100.0% | 0.0% | n/a | n/a | 0.0% | 8.3% | -8.3 | 0.0% | -0.2 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 3 | 17 | 15 | 0 | 100.0% | -497 | 74 |
| `U69` | 36 | 18.2% | 85.7% | 30.0% | 12 | 50.0% | 45.5% | 20.0% | 99.6% | 33.4% | 44.4% | 27.8% | +16.7 | 45.5% | +19.7 | 0.01 | 0.20 | 44.4% | 6 | 0 | 13 | 5 | 1 | 66.7% | 24,660 | 68 |
| `ACS_TIMEOUT` | 34 | 3.3% | 100.0% | 6.5% | 3 | 33.3% | 66.7% | 0.8% | 100.0% | 1.5% | 44.1% | 41.2% | +2.9 | 67.2% | +0.6 | 0.01 | 0.16 | 44.1% | 1 | 0 | 14 | 2 | 0 | 100.0% | 1,351 | 58 |

#### `rules_only` by failure code

| failure code | n | precision | recall | F1 | addressable | capture | selection error | precision (INR-wt) | recall (INR-wt) | F1 (INR-wt) | recovery | organic | uplift pp | recovery (INR-wt) | uplift pp (INR-wt) | median days | mean days | recovered <=72h | incr | cannib | hopeless | wrong action | missed | restraint precision | net INR | attempts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `51` | 360 | 27.4% | 98.9% | 43.0% | 134 | 69.4% | 30.1% | 26.9% | 94.5% | 41.9% | 53.3% | 28.9% | +24.4 | 42.4% | +22.0 | 8.22 | 10.64 | 8.1% | 93 | 5 | 108 | 40 | 1 | 95.2% | 284,537 | 336 |
| `91` | 129 | 38.3% | 93.9% | 54.4% | 64 | 71.9% | 24.6% | 39.7% | 99.4% | 56.7% | 58.1% | 22.5% | +35.7 | 75.5% | +39.2 | 1.01 | 1.02 | 58.1% | 46 | 0 | 30 | 15 | 3 | 66.7% | 199,443 | 117 |
| `41` | 57 | n/a | 0.0% | n/a | 22 | 0.0% | n/a | n/a | 0.0% | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 0 | 0 | 22 | 61.4% | 0 | 0 |
| `96` | 56 | 48.1% | 100.0% | 64.9% | 25 | 100.0% | 0.0% | 35.5% | 100.0% | 52.4% | 71.4% | 26.8% | +44.6 | 64.0% | +35.2 | 0.09 | 0.10 | 71.4% | 25 | 0 | 13 | 0 | 0 | 100.0% | 129,138 | 52 |
| `R05` | 41 | n/a | 0.0% | n/a | 10 | 0.0% | n/a | n/a | 0.0% | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 0 | 0 | 10 | 75.6% | 0 | 0 |
| `MANDATE_EXPIRED_M06` | 36 | 38.9% | 100.0% | 56.0% | 15 | 93.3% | 6.7% | 36.9% | 100.0% | 53.9% | 47.2% | 8.3% | +38.9 | 37.1% | +36.9 | 1.95 | 2.15 | 41.7% | 14 | 0 | 18 | 1 | 0 | n/a | 74,291 | 0 |
| `U69` | 36 | 27.3% | 90.0% | 41.9% | 12 | 75.0% | 18.2% | 20.9% | 99.6% | 34.6% | 52.8% | 27.8% | +25.0 | 46.4% | +20.6 | 0.10 | 0.19 | 52.8% | 9 | 0 | 13 | 2 | 1 | 66.7% | 25,758 | 33 |
| `ACS_TIMEOUT` | 34 | 6.7% | 100.0% | 12.5% | 3 | 66.7% | 33.3% | 1.5% | 100.0% | 2.9% | 47.1% | 41.2% | +5.9 | 67.8% | +1.2 | 0.34 | 0.46 | 47.1% | 2 | 0 | 14 | 1 | 0 | 100.0% | 2,619 | 30 |

#### `retry_economist (naive plan)` by failure code

| failure code | n | precision | recall | F1 | addressable | capture | selection error | precision (INR-wt) | recall (INR-wt) | F1 (INR-wt) | recovery | organic | uplift pp | recovery (INR-wt) | uplift pp (INR-wt) | median days | mean days | recovered <=72h | incr | cannib | hopeless | wrong action | missed | restraint precision | net INR | attempts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `51` | 360 | 16.4% | 20.4% | 18.2% | 134 | 15.7% | 59.6% | 8.7% | 65.1% | 15.4% | 32.5% | 28.9% | +3.6 | 25.2% | +4.8 | 0.60 | 0.94 | 32.5% | 21 | 8 | 43 | 31 | 82 | 64.7% | 62,200 | 253 |
| `91` | 129 | 43.0% | 60.7% | 50.4% | 64 | 53.1% | 19.0% | 25.0% | 96.2% | 39.7% | 48.1% | 22.5% | +25.6 | 60.3% | +24.0 | 0.02 | 0.20 | 48.1% | 34 | 1 | 15 | 8 | 22 | 56.0% | 122,240 | 132 |
| `41` | 57 | n/a | 0.0% | n/a | 22 | 0.0% | n/a | n/a | 0.0% | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 0 | 0 | 22 | 61.4% | 0 | 0 |
| `96` | 56 | 46.7% | 87.5% | 60.9% | 25 | 84.0% | 4.5% | 35.2% | 99.7% | 52.0% | 64.3% | 26.8% | +37.5 | 63.6% | +34.8 | 0.01 | 0.06 | 64.3% | 21 | 0 | 10 | 1 | 3 | 72.7% | 127,620 | 63 |
| `R05` | 41 | n/a | 0.0% | n/a | 10 | 0.0% | n/a | n/a | 0.0% | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 0 | 0 | 10 | 75.6% | 0 | 0 |
| `MANDATE_EXPIRED_M06` | 36 | n/a | 0.0% | n/a | 15 | 0.0% | n/a | n/a | 0.0% | n/a | 8.3% | 8.3% | +0.0 | 0.2% | +0.0 | 2.00 | 1.77 | 8.3% | 0 | 0 | 0 | 0 | 15 | 58.3% | 0 | 0 |
| `U69` | 36 | 22.2% | 36.4% | 27.6% | 12 | 33.3% | 20.0% | 20.0% | 89.4% | 32.6% | 38.9% | 27.8% | +11.1 | 44.7% | +18.9 | 0.10 | 0.70 | 38.9% | 4 | 0 | 9 | 1 | 7 | 61.1% | 23,572 | 38 |
| `ACS_TIMEOUT` | 34 | 5.9% | 50.0% | 10.5% | 3 | 33.3% | 50.0% | 0.8% | 69.5% | 1.6% | 44.1% | 41.2% | +2.9 | 67.2% | +0.6 | 0.02 | 0.61 | 44.1% | 1 | 0 | 6 | 1 | 1 | 94.1% | 1,351 | 27 |

#### `retry_economist (prior)` by failure code

| failure code | n | precision | recall | F1 | addressable | capture | selection error | precision (INR-wt) | recall (INR-wt) | F1 (INR-wt) | recovery | organic | uplift pp | recovery (INR-wt) | uplift pp (INR-wt) | median days | mean days | recovered <=72h | incr | cannib | hopeless | wrong action | missed | restraint precision | net INR | attempts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `51` | 360 | 27.7% | 90.7% | 42.4% | 134 | 65.7% | 29.6% | 26.9% | 94.0% | 41.9% | 51.9% | 28.9% | +23.1 | 42.4% | +21.9 | 7.93 | 10.23 | 9.4% | 88 | 5 | 101 | 37 | 9 | 78.6% | 284,042 | 318 |
| `91` | 129 | 39.3% | 92.0% | 55.1% | 64 | 71.9% | 23.3% | 39.8% | 99.3% | 56.8% | 58.1% | 22.5% | +35.7 | 75.5% | +39.2 | 1.01 | 1.01 | 58.1% | 46 | 0 | 29 | 14 | 4 | 66.7% | 199,443 | 117 |
| `41` | 57 | n/a | 0.0% | n/a | 22 | 0.0% | n/a | n/a | 0.0% | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 0 | 0 | 22 | 61.4% | 0 | 0 |
| `96` | 56 | 48.1% | 100.0% | 64.9% | 25 | 100.0% | 0.0% | 35.5% | 100.0% | 52.4% | 71.4% | 26.8% | +44.6 | 64.0% | +35.2 | 0.09 | 0.10 | 71.4% | 25 | 0 | 13 | 0 | 0 | 100.0% | 129,138 | 52 |
| `R05` | 41 | n/a | 0.0% | n/a | 10 | 0.0% | n/a | n/a | 0.0% | n/a | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a (0 recovered) | n/a (0 recovered) | 0.0% | 0 | 0 | 0 | 0 | 10 | 75.6% | 0 | 0 |
| `MANDATE_EXPIRED_M06` | 36 | 40.0% | 26.7% | 32.0% | 15 | 26.7% | 0.0% | 41.6% | 90.2% | 57.0% | 19.4% | 8.3% | +11.1 | 33.9% | +33.6 | 2.00 | 2.03 | 16.7% | 4 | 0 | 6 | 0 | 11 | 57.7% | 67,832 | 0 |
| `U69` | 36 | 31.6% | 54.5% | 40.0% | 12 | 50.0% | 14.3% | 27.0% | 45.8% | 34.0% | 44.4% | 27.8% | +16.7 | 45.2% | +19.4 | 0.13 | 0.66 | 44.4% | 6 | 0 | 9 | 1 | 5 | 70.6% | 24,278 | 19 |
| `ACS_TIMEOUT` | 34 | 11.1% | 33.3% | 16.7% | 3 | 33.3% | 0.0% | 1.7% | 39.5% | 3.2% | 44.1% | 41.2% | +2.9 | 67.1% | +0.6 | 0.47 | 1.06 | 44.1% | 1 | 0 | 2 | 0 | 2 | 92.0% | 1,268 | 9 |

#### `oracle_best (CHEATS)` by failure code

| failure code | n | precision | recall | F1 | addressable | capture | selection error | precision (INR-wt) | recall (INR-wt) | F1 (INR-wt) | recovery | organic | uplift pp | recovery (INR-wt) | uplift pp (INR-wt) | median days | mean days | recovered <=72h | incr | cannib | hopeless | wrong action | missed | restraint precision | net INR | attempts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `51` | 360 | 100.0% | 100.0% | 100.0% | 134 | 100.0% | 0.0% | 100.0% | 100.0% | 100.0% | 66.1% | 28.9% | +37.2 | 50.6% | +30.2 | 1.40 | 2.99 | 50.0% | 134 | 0 | 0 | 0 | 0 | 100.0% | 390,583 | 133 |
| `91` | 129 | 100.0% | 100.0% | 100.0% | 64 | 100.0% | 0.0% | 100.0% | 100.0% | 100.0% | 72.1% | 22.5% | +49.6 | 78.3% | +42.1 | 0.35 | 1.23 | 65.9% | 64 | 0 | 0 | 0 | 0 | 100.0% | 213,932 | 61 |
| `41` | 57 | 100.0% | 100.0% | 100.0% | 22 | 100.0% | 0.0% | 100.0% | 100.0% | 100.0% | 38.6% | 0.0% | +38.6 | 12.4% | +12.4 | 2.10 | 2.21 | 35.1% | 22 | 0 | 0 | 0 | 0 | 100.0% | 15,922 | 0 |
| `96` | 56 | 100.0% | 100.0% | 100.0% | 25 | 100.0% | 0.0% | 100.0% | 100.0% | 100.0% | 71.4% | 26.8% | +44.6 | 64.0% | +35.2 | 0.02 | 0.52 | 71.4% | 25 | 0 | 0 | 0 | 0 | 100.0% | 129,138 | 25 |
| `R05` | 41 | 100.0% | 100.0% | 100.0% | 10 | 100.0% | 0.0% | 100.0% | 100.0% | 100.0% | 24.4% | 0.0% | +24.4 | 16.6% | +16.6 | 2.00 | 2.00 | 24.4% | 10 | 0 | 0 | 0 | 0 | 100.0% | 116,579 | 0 |
| `MANDATE_EXPIRED_M06` | 36 | 100.0% | 100.0% | 100.0% | 15 | 100.0% | 0.0% | 100.0% | 100.0% | 100.0% | 50.0% | 8.3% | +41.7 | 37.5% | +37.3 | 1.32 | 1.32 | 47.2% | 15 | 0 | 0 | 0 | 0 | 100.0% | 75,181 | 7 |
| `U69` | 36 | 100.0% | 100.0% | 100.0% | 12 | 100.0% | 0.0% | 100.0% | 100.0% | 100.0% | 61.1% | 27.8% | +33.3 | 68.7% | +42.9 | 0.40 | 0.85 | 61.1% | 12 | 0 | 0 | 0 | 0 | 100.0% | 53,547 | 11 |
| `ACS_TIMEOUT` | 34 | 100.0% | 100.0% | 100.0% | 3 | 100.0% | 0.0% | 100.0% | 100.0% | 100.0% | 50.0% | 41.2% | +8.8 | 68.0% | +1.5 | 1.23 | 1.32 | 50.0% | 3 | 0 | 0 | 0 | 0 | 100.0% | 3,213 | 3 |

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
