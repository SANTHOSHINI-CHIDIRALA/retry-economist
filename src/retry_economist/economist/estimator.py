"""Where the two recovery probabilities come from, behind one interface.

The economist's arithmetic needs `p_recover_if_act` and `p_recover_if_abstain`
for a transaction; it must not care which of two very different processes
produced them. `Estimator` is that seam. Two implementations sit behind it:

- `RouterEstimator` - the number the LLM (or the mock stand-in) put in its own
  `Proposal`. Whatever confidence it has is whatever confidence it has;
  nothing here second-guesses it.
- `HistoricalPriorEstimator` - a per-failure-code rate, fitted once from the
  TRAIN split by `eval/calibration.py::HistoricalPrior.fit`. That module lives
  under `eval/`, which this package is forbidden from importing (see
  `costs.py`'s docstring), so this class takes the fitted numbers as plain
  dicts and floats rather than importing the class that produced them. The
  caller - `eval/cli.py`, which is not guarded - is what will bridge the two,
  by unpacking a `HistoricalPrior` into these plain arguments.

Phase 4 found the router's own probabilities LOSE to this historical prior on
both estimates (see `docs/PROGRESS.md`, Phase 4). That is exactly why this
module refuses to pick a default: which `Estimator` an `Economist` uses is a
decision for whoever wires the two phases together, made from the calibration
numbers of the run that is actually happening, not assumed in advance here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Tuple, runtime_checkable

from retry_economist.policies.base import ObservedTransaction


@runtime_checkable
class Estimator(Protocol):
    """Anything that can price a transaction's two recovery probabilities."""

    #: Travels into the audit record, so a decision always says which source
    #: of probabilities produced it.
    label: str

    def estimate(self, txn: ObservedTransaction, proposal: Any) -> Tuple[float, float]:
        """Returns `(p_recover_if_act, p_recover_if_abstain)`."""
        ...


@dataclass(frozen=True, slots=True)
class RouterEstimator:
    """Reads the two probabilities straight off the proposal that was scored."""

    label: str = "router"

    def estimate(self, txn: ObservedTransaction, proposal: Any) -> Tuple[float, float]:
        return float(proposal.p_recover_if_act), float(proposal.p_recover_if_abstain)


@dataclass(frozen=True, slots=True)
class HistoricalPriorEstimator:
    """A per-failure-code rate fitted on train data, supplied as plain data.

    Mirrors `eval/calibration.py::HistoricalPrior`'s prediction rules exactly
    (`predict_act` / `predict_abstain`) so scoring the same estimator two ways
    - once inside the calibration report, once inside a live decision - can
    never disagree. The plan-aware lookup only matters for `p_recover_if_act`:
    `p_recover_if_abstain` does not depend on any proposed action, since it is
    asking what happens if nothing is done at all.
    """

    abstain_by_code: Mapping[str, float]
    act_by_code_action: Mapping[Tuple[str, str], float]
    act_by_code: Mapping[str, float]
    global_abstain: float
    global_act: float
    label: str = "historical_prior_train_only"

    def estimate(self, txn: ObservedTransaction, proposal: Any) -> Tuple[float, float]:
        p_abstain = self.abstain_by_code.get(txn.failure_code, self.global_abstain)

        plan = tuple(getattr(proposal, "proposed_plan", ()) or ())
        if plan:
            key = (txn.failure_code, plan[0])
            if key in self.act_by_code_action:
                p_act = self.act_by_code_action[key]
            else:
                p_act = self.act_by_code.get(txn.failure_code, self.global_act)
        else:
            p_act = self.act_by_code.get(txn.failure_code, self.global_act)

        return float(p_act), float(p_abstain)
