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

| policy | status | recovery rate (95% CI) | net uplift pp (95% CI) | action rate | abstained | restraint precision | net INR | cost INR | net value INR | INR spent per INR earned | attempts | contact | viol |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `do_nothing` | ok | 23.4% [20.4, 26.3] | +0.0 [+0.0, +0.0] | 0.0% | 749 | 61.9% | 0 | 0 | 0 | n/a (no net revenue) | 0 | 0.0% | 0 |

## Reference bounds - NOT RESULTS

These read the counterfactual outcomes to pick an action already known to work.
No deployable policy can do this; they are here to show how much was ever
available to win.

| policy | status | recovery rate (95% CI) | net uplift pp (95% CI) | action rate | abstained | restraint precision | net INR | cost INR | net value INR | INR spent per INR earned | attempts | contact | viol |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `oracle_best (CHEATS)` | ok | 61.4% [57.4, 65.4] | +38.1 [+34.7, +41.4] | 38.1% | 464 | 100.0% | 998,095 | 16,612 | 981,483 | 0.02 | 240 | 7.9% | 0 |

## Attribution

Split first on whether the policy acted at all. Restraint is not failure: leaving a customer alone who pays unaided, or one no available action could have recovered, is the system working - at zero cost. **Restraint precision** is the share of untouched transactions that fall in those two buckets.

| policy | acted | incremental | cannibalised | wasted | futile | abstained | correct restraint | correct walkaway | missed opportunity | sum |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `do_nothing` | 0 | 0 | 0 | 0 | 0 | 749 | 175 | 289 | 285 | 749/749 |
| `oracle_best (CHEATS)` | 285 | 285 | 0 | 0 | 0 | 464 | 175 | 289 | 0 | 749/749 |

## Breakdown by failure code

#### `do_nothing` by failure code

| failure code | n | recovery | organic | uplift pp | incr | cannib | missed | restraint precision | net INR | attempts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `51` | 360 | 28.9% | 28.9% | +0.0 | 0 | 0 | 134 | 62.8% | 0 | 0 |
| `91` | 129 | 22.5% | 22.5% | +0.0 | 0 | 0 | 64 | 50.4% | 0 | 0 |
| `41` | 57 | 0.0% | 0.0% | +0.0 | 0 | 0 | 22 | 61.4% | 0 | 0 |
| `96` | 56 | 26.8% | 26.8% | +0.0 | 0 | 0 | 25 | 55.4% | 0 | 0 |
| `R05` | 41 | 0.0% | 0.0% | +0.0 | 0 | 0 | 10 | 75.6% | 0 | 0 |
| `MANDATE_EXPIRED_M06` | 36 | 8.3% | 8.3% | +0.0 | 0 | 0 | 15 | 58.3% | 0 | 0 |
| `U69` | 36 | 27.8% | 27.8% | +0.0 | 0 | 0 | 12 | 66.7% | 0 | 0 |
| `ACS_TIMEOUT` | 34 | 41.2% | 41.2% | +0.0 | 0 | 0 | 3 | 91.2% | 0 | 0 |

#### `oracle_best (CHEATS)` by failure code

| failure code | n | recovery | organic | uplift pp | incr | cannib | missed | restraint precision | net INR | attempts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `51` | 360 | 66.1% | 28.9% | +37.2 | 134 | 0 | 0 | 100.0% | 390,583 | 133 |
| `91` | 129 | 72.1% | 22.5% | +49.6 | 64 | 0 | 0 | 100.0% | 213,932 | 61 |
| `41` | 57 | 38.6% | 0.0% | +38.6 | 22 | 0 | 0 | 100.0% | 15,922 | 0 |
| `96` | 56 | 71.4% | 26.8% | +44.6 | 25 | 0 | 0 | 100.0% | 129,138 | 25 |
| `R05` | 41 | 24.4% | 0.0% | +24.4 | 10 | 0 | 0 | 100.0% | 116,579 | 0 |
| `MANDATE_EXPIRED_M06` | 36 | 50.0% | 8.3% | +41.7 | 15 | 0 | 0 | 100.0% | 75,181 | 7 |
| `U69` | 36 | 61.1% | 27.8% | +33.3 | 12 | 0 | 0 | 100.0% | 53,547 | 11 |
| `ACS_TIMEOUT` | 34 | 50.0% | 41.2% | +8.8 | 3 | 0 | 0 | 100.0% | 3,213 | 3 |

## Sensitivity to customer lifetime value

Customer lifetime value is an ASSUMPTION, not a measurement, and annoyance cost scales linearly with it. Each policy below is re-priced across a range wide enough to cover any plausible value. Net *revenue* is invariant by construction - no churn assumption touches it - so net *value*, which subtracts what the recovery cost, is the column that moves.

> Every policy keeps the sign of its net value across the whole sweep.

| policy | CLV (INR) | net revenue INR | annoyance cost INR | net value INR | INR spent per INR earned |
| --- | ---: | ---: | ---: | ---: | ---: |
| `do_nothing` | 4,000 | 0 | 0 | 0 | n/a (no net revenue) |
| `do_nothing` | 12,000 | 0 | 0 | 0 | n/a (no net revenue) |
| `do_nothing` | 30,000 | 0 | 0 | 0 | n/a (no net revenue) |
| `oracle_best (CHEATS) (bound)` | 4,000 | 998,095 | 5,178 | 991,838 | 0.0063 |
| `oracle_best (CHEATS) (bound)` | 12,000 | 998,095 | 15,533 | 981,483 | 0.02 |
| `oracle_best (CHEATS) (bound)` | 30,000 | 998,095 | 38,832 | 958,183 | 0.04 |

| policy | verdict |
| --- | --- |
| `do_nothing` | robust - net value keeps its sign across the whole range |
| `oracle_best (CHEATS)` | robust - net value keeps its sign across the whole range |

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
