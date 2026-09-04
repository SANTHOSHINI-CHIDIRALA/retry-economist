"""The router: three deterministic signals, one LLM call, one Proposal out.

The router PROPOSES. It cannot execute: a `Proposal` is not a `Decision`, the
simulator only accepts a `Decision`, and nothing in this package is permitted to
construct one. That separation is what lets the economist layer in a later phase
sit between the model and the money.

Nothing here may read counterfactual outcome data; the leakage guard in the test
suite walks this package.
"""
