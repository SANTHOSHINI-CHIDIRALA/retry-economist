# retry-economist

Failed-payment recovery agent that treats every retry as a purchase decision.
See `docs/PROGRESS.md` for the full build log, results, and how to reproduce
every number.

## Scope: audit trail only, no execution

Phase 6 (`src/retry_economist/audit/`) gives the economist's decisions a
durable, append-only audit trail - one JSONL record per decision, with full
provenance (signals, proposal, every compliance rule checked, the itemised EV
terms, the verdict, and a deterministic idempotency key per authorised
action). **It does not execute anything.** There is no HTTP client anywhere
in this codebase and none is planned here: wiring an authorised plan to a
real Razorpay (or any other) API call is explicitly out of scope for this
project. The ledger exists so that IF an execution layer were built later, it
would have something auditable to build on top of - not because one exists.

