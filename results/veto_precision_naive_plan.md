# retry_economist (naive plan) - compliance & veto analysis, full holdout (749 txns)

## 1. Compliance rule firing counts

- transactions with a non-empty naive_retry_3x proposal: 702 / 749
- transactions with no proposal (attempts_left == 0): 47

| rule | fired on n transactions |
| --- | ---: |
| `C1_RISK_DECLINED` | 39 |
| `C2_HARD_DECLINE_NO_DEBIT` | 54 |
| `C3_ATTEMPT_CAP` | 0 |
| `C4_EXPIRED_MANDATE` | 41 |
| `C5_CONTACT_CAP` | 0 |

## 2. Verdict counts

| verdict | n |
| --- | ---: |
| `approve` | 287 |
| `approve_truncated` | 0 |
| `veto` | 415 |

## 3. hard_decline_retry_waste

Debit attempts spent retrying an instrument the acquirer has already classed as a hard decline (blocked, closed, fraud-flagged) - no retry on any rail can ever clear these; every attempt is a fee and an annoyance cost spent on a foregone conclusion.

| policy | hard_decline_retry_waste |
| --- | ---: |
| `naive_retry_3x` | 245 |
| `retry_economist (naive plan)` | 0 |

> `naive_retry_3x` burns **245** debit attempts on dead instruments; `retry_economist (naive plan)`, given the IDENTICAL proposed ladder for every one of those same transactions, reduces this to **0**. This delta is attributable ENTIRELY to the economist's C2 (hard-decline) and C1 (risk-decline) compliance rules, since the plan source did no discriminating at all.

## 4. Veto precision

Of the actions `naive_retry_3x` proposed that the economist removed, what share would have FAILED anyway per the oracle's counterfactual outcome for that exact action?

| split | n vetoed actions | would have failed anyway | veto precision |
| --- | ---: | ---: | ---: |
| all vetoes | 1115 | 796 | 71.4% |
| compliance-driven (C1-C5) | 335 | 329 | 98.2% |
| economics-driven (EV<=0) | 780 | 467 | 59.9% |

| rule | n vetoed actions | would have failed anyway | veto precision |
| --- | ---: | ---: | ---: |
| `C1_RISK_DECLINED` | 101 | 101 | 100.0% |
| `C2_HARD_DECLINE_NO_DEBIT` | 144 | 144 | 100.0% |
| `C4_EXPIRED_MANDATE` | 90 | 84 | 93.3% |
| `EV<=0` | 780 | 467 | 59.9% |

