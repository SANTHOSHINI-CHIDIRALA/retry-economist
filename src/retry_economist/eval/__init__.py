"""The evaluation harness: the scoreboard, and nothing else.

Phase 2 builds only measurement. There is no router, no economist layer and no
execution layer here - just the machinery that takes a policy, runs it against
held-out failures, and reports what it actually bought.

Nothing under `retry_economist.policies` may import this package; see the
isolation rule in `policies/base.py`.
"""
