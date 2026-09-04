# Veto precision - retry_economist (prior), full holdout (749 txns)

Of the actions `rules_only` proposed that the economist removed (never executed), what share would have FAILED anyway per the oracle's counterfactual outcome for that exact action? That is veto precision - a vetoed action that would have failed cost the merchant nothing to skip. The complement is the real price of caution: a vetoed action that WOULD have recovered the payment.

- transactions where `rules_only` proposed an action: **610** / 749
  - fully approved (verdict `approve`): 525
  - approved a truncated/filtered prefix (`approve_truncated`): 0
  - fully vetoed (`veto`): 85
- transactions where `rules_only` itself proposed nothing (not scored here): 139

## Overall

| split | n vetoed actions | would have failed anyway | veto precision |
| --- | ---: | ---: | ---: |
| all vetoes | 85 | 43 | 50.6% |
| compliance-driven (C1-C5) | 30 | 14 | 46.7% |
| economics-driven (EV<=0) | 55 | 29 | 52.7% |

## By rule

| rule | n vetoed actions | would have failed anyway | veto precision |
| --- | ---: | ---: | ---: |
| `C5_CONTACT_CAP` | 30 | 14 | 46.7% |
| `EV<=0` | 55 | 29 | 52.7% |

> A rule with 100% precision never blocked a would-have-worked action on this holdout. A rule below 100% has a real cost: it vetoed at least one action that the oracle says would have recovered the payment - the price the merchant pays for that rule's protection.
