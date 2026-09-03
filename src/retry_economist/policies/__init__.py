"""Recovery policies: the things being scored.

A policy is anything that turns one observed failed payment into an ordered
plan of actions. The contract lives in `base.py`, and the isolation rule stated
there - a policy sees the observed transaction and nothing else - is the reason
any number this harness produces can be believed.
"""
