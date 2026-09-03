"""Synthetic failed-payment world for the Retry Economist benchmark.

Build order is deliberate and one-directional:

    world  ->  customers  ->  failures (transactions)  ->  outcomes (oracle)

Each stage may read the stages before it and never the stages after it. That is
what keeps the causal story honest: a failure is *caused* by the customer's
liquidity and the issuer's health at that instant, so a router that recovers the
underlying cause genuinely outperforms one that retries blindly.
"""

from retry_economist.generator.world import World, build_world  # noqa: F401
