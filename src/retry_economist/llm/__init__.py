"""LLM access: one provider protocol, two implementations, one cache in front.

Nothing in this package may read counterfactual outcome data. It sits behind the
router, which sits behind a policy, and the isolation rule from
`policies/base.py` applies transitively - the leakage guard in the test suite
walks this package too.
"""
