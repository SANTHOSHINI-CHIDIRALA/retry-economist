# Retry Economist - scoreboard (holdout)

- split: **holdout**
- transactions: **749**
- customers: **240**
- data seed: `42`
- generator hash: `92fad1da7dfc`
- bootstrap: 2000 clustered iterations (resampling customers), seed `20260601`
- recoverable ceiling: **61.4%** within debit-attempt caps, 61.7% ignoring them (scheme caps cost 0.3 pp of recovery outright)

## HEADLINE

> HEADLINE: not available - no baseline policy in this run

## Results

| policy | status | recovery rate (95% CI) | net uplift pp (95% CI) | recovery (INR-wt) | uplift pp (INR-wt) | median days | mean days | recovered <=72h | action rate | abstained | restraint precision | net INR | cost INR | net value INR | INR spent per INR earned | attempts | contact | viol |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `do_nothing` | ok | 23.4% [20.4, 26.3] | +0.0 [+0.0, +0.0] | 20.6% | +0.0 | 1.48 | 1.51 | 23.4% | 0.0% | 749 | 61.9% | 0 | 0 | 0 | n/a (no net revenue) | 0 | 0.0% | 0 |

## Reference bounds - NOT RESULTS

These read the counterfactual outcomes to pick an action already known to work.
No deployable policy can do this; they are here to show how much was ever
available to win.

| policy | status | recovery rate (95% CI) | net uplift pp (95% CI) | recovery (INR-wt) | uplift pp (INR-wt) | median days | mean days | recovered <=72h | action rate | abstained | restraint precision | net INR | cost INR | net value INR | INR spent per INR earned | attempts | contact | viol |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `oracle_best (CHEATS)` | ok | 61.4% [57.4, 65.4] | +38.1 [+34.7, +41.4] | 48.8% | +28.2 | 1.01 | 2.13 | 52.2% | 38.1% | 464 | 100.0% | 998,095 | 16,612 | 981,483 | 0.02 | 240 | 7.9% | 0 |

## Attribution

Split first on whether the policy acted at all. Restraint is not failure: leaving a customer alone who pays unaided, or one no available action could have recovered, is the system working - at zero cost. **Restraint precision** is the share of untouched transactions that fall in those two buckets.

| policy | acted | incremental | cannibalised | wasted | futile | abstained | correct restraint | correct walkaway | missed opportunity | sum |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `do_nothing` | 0 | 0 | 0 | 0 | 0 | 749 | 175 | 289 | 285 | 749/749 |
| `oracle_best (CHEATS)` | 285 | 285 | 0 | 0 | 0 | 464 | 175 | 289 | 0 | 749/749 |

### Weighted by rupees at risk

The same seven buckets as a share of the money, not of the invoice count. Amounts here span a median around INR 700 to a p95 above INR 31,000, so the two views can rank policies differently - and where they diverge, the divergence is the finding, not a rounding artefact.

| policy | at risk INR | incremental | cannibalised | wasted | futile | correct restraint | correct walkaway | missed opportunity | sum |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `do_nothing` | 3,544,846 | 0.0% | 0.0% | 0.0% | 0.0% | 20.6% | 51.2% | 28.2% | 100.0% |
| `oracle_best (CHEATS)` | 3,544,846 | 28.2% | 0.0% | 0.0% | 0.0% | 20.6% | 51.2% | 0.0% | 100.0% |

## Breakdown by failure code

#### `do_nothing` by failure code

| failure code | n | recovery | organic | uplift pp | recovery (INR-wt) | uplift pp (INR-wt) | median days | mean days | recovered <=72h | incr | cannib | missed | restraint precision | net INR | attempts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `51` | 360 | 28.9% | 28.9% | +0.0 | 20.4% | +0.0 | 1.42 | 1.51 | 28.9% | 0 | 0 | 134 | 62.8% | 0 | 0 |
| `91` | 129 | 22.5% | 22.5% | +0.0 | 36.2% | +0.0 | 1.69 | 1.52 | 22.5% | 0 | 0 | 64 | 50.4% | 0 | 0 |
| `41` | 57 | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a | n/a | 0.0% | 0 | 0 | 22 | 61.4% | 0 | 0 |
| `96` | 56 | 26.8% | 26.8% | +0.0 | 28.8% | +0.0 | 1.38 | 1.35 | 26.8% | 0 | 0 | 25 | 55.4% | 0 | 0 |
| `R05` | 41 | 0.0% | 0.0% | +0.0 | 0.0% | +0.0 | n/a | n/a | 0.0% | 0 | 0 | 10 | 75.6% | 0 | 0 |
| `MANDATE_EXPIRED_M06` | 36 | 8.3% | 8.3% | +0.0 | 0.2% | +0.0 | 2.00 | 1.77 | 8.3% | 0 | 0 | 15 | 58.3% | 0 | 0 |
| `U69` | 36 | 27.8% | 27.8% | +0.0 | 25.8% | +0.0 | 1.70 | 1.52 | 27.8% | 0 | 0 | 12 | 66.7% | 0 | 0 |
| `ACS_TIMEOUT` | 34 | 41.2% | 41.2% | +0.0 | 66.5% | +0.0 | 1.76 | 1.57 | 41.2% | 0 | 0 | 3 | 91.2% | 0 | 0 |

#### `oracle_best (CHEATS)` by failure code

| failure code | n | recovery | organic | uplift pp | recovery (INR-wt) | uplift pp (INR-wt) | median days | mean days | recovered <=72h | incr | cannib | missed | restraint precision | net INR | attempts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `51` | 360 | 66.1% | 28.9% | +37.2 | 50.6% | +30.2 | 1.40 | 2.99 | 50.0% | 134 | 0 | 0 | 100.0% | 390,583 | 133 |
| `91` | 129 | 72.1% | 22.5% | +49.6 | 78.3% | +42.1 | 0.35 | 1.23 | 65.9% | 64 | 0 | 0 | 100.0% | 213,932 | 61 |
| `41` | 57 | 38.6% | 0.0% | +38.6 | 12.4% | +12.4 | 2.10 | 2.21 | 35.1% | 22 | 0 | 0 | 100.0% | 15,922 | 0 |
| `96` | 56 | 71.4% | 26.8% | +44.6 | 64.0% | +35.2 | 0.02 | 0.52 | 71.4% | 25 | 0 | 0 | 100.0% | 129,138 | 25 |
| `R05` | 41 | 24.4% | 0.0% | +24.4 | 16.6% | +16.6 | 2.00 | 2.00 | 24.4% | 10 | 0 | 0 | 100.0% | 116,579 | 0 |
| `MANDATE_EXPIRED_M06` | 36 | 50.0% | 8.3% | +41.7 | 37.5% | +37.3 | 1.32 | 1.32 | 47.2% | 15 | 0 | 0 | 100.0% | 75,181 | 7 |
| `U69` | 36 | 61.1% | 27.8% | +33.3 | 68.7% | +42.9 | 0.40 | 0.85 | 61.1% | 12 | 0 | 0 | 100.0% | 53,547 | 11 |
| `ACS_TIMEOUT` | 34 | 50.0% | 41.2% | +8.8 | 68.0% | +1.5 | 1.23 | 1.32 | 50.0% | 3 | 0 | 0 | 100.0% | 3,213 | 3 |

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
