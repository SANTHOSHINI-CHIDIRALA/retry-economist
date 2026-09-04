"""Contract tests for the LLM layer, the router, and the ablation policy.

Every one of these runs offline with no API key. The properties they guard are
the ones that would be dangerous to get wrong:

- the router PROPOSES; it cannot execute, and it cannot construct a Decision;
- a model returning garbage degrades to doing nothing, never to acting;
- a cache hit never touches the network;
- and nothing in the LLM or router packages can reach the counterfactual store.
"""

from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from retry_economist.llm.cache import ResponseCache, cache_key  # noqa: E402
from retry_economist.llm.provider import (  # noqa: E402
    BrokenProvider,
    CachingProvider,
    MockProvider,
    ParseFailure,
    ProviderUnavailable,
)
from retry_economist.policies.base import Decision, ObservedTransaction, validate_plan  # noqa: E402
from retry_economist.policies.llm_router_only import LLMRouterOnlyPolicy  # noqa: E402
from retry_economist.router.router import RESPONSE_SCHEMA, Proposal, Router, build_prompt  # noqa: E402
from retry_economist.router.signals import SignalIndex  # noqa: E402
from test_signals import make_txn  # noqa: E402


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this file must pass with no key present."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


@pytest.fixture
def router(tmp_path: Path) -> Router:
    txns = [make_txn(txn_id=f"t{i}", customer_id=f"c{i % 3}") for i in range(6)]
    provider = CachingProvider(MockProvider(), ResponseCache(tmp_path / "cache"))
    return Router(provider, SignalIndex(txns))


# ---------------------------------------------------------------------------
# the proposal is not a decision
# ---------------------------------------------------------------------------


def test_a_proposal_is_not_a_decision_and_cannot_execute(router: Router) -> None:
    """The type system carries the separation, not a comment.

    The simulator only accepts a `Decision`; a `Proposal` is a different type
    with different attribute names, so it cannot be smuggled through even by a
    caller that is not paying attention.
    """
    proposal = router.propose(make_txn())

    assert isinstance(proposal, Proposal)
    assert not isinstance(proposal, Decision)
    # The attribute names do not line up either, so duck typing cannot bridge it.
    assert not hasattr(proposal, "plan")
    assert not hasattr(proposal, "reason")
    assert hasattr(proposal, "proposed_plan")

    from retry_economist.policies.base import InvalidPlan, validated_decision

    class _LeakyPolicy:
        name = "leaky"

        def decide(self, txn: ObservedTransaction):
            return proposal  # a Proposal where a Decision is required

    with pytest.raises(InvalidPlan, match="must return a Decision"):
        validated_decision(_LeakyPolicy(), make_txn())


def test_router_module_never_constructs_a_decision() -> None:
    """Walk the syntax tree: no route from the model to an executable object.

    Checked structurally rather than by reading the file, because the rule has
    to survive future edits by people who have not read this docstring.
    """
    for path in sorted((SRC / "retry_economist" / "router").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id != "Decision", f"{path.name} references Decision"
            if isinstance(node, ast.Attribute):
                assert node.attr != "Decision", f"{path.name} references Decision"
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    assert alias.name != "Decision", f"{path.name} imports Decision"


def test_proposals_are_well_formed(router: Router) -> None:
    proposal = router.propose(make_txn())
    assert 0.0 <= proposal.p_recover_if_act <= 1.0
    assert 0.0 <= proposal.p_recover_if_abstain <= 1.0
    assert 0.0 <= proposal.root_cause_confidence <= 1.0
    assert proposal.rationale.strip()
    # A rationale that cites no signal is decoration; the schema demands one.
    assert any(
        name in proposal.rationale
        for name in ("root_cause", "issuer_health_now", "liquidity_timing")
    )
    # Whatever it proposes must be a legal plan.
    validate_plan(proposal.proposed_plan, policy_name="router")
    json.dumps(proposal.to_dict())  # must survive serialisation


def test_router_never_proposes_more_attempts_than_the_budget(router: Router) -> None:
    from retry_economist.schema import ATTEMPTS_CONSUMED

    for used, cap in ((0, 3), (2, 3), (3, 3), (5, 5)):
        txn = make_txn(retry_attempts_used=used, retry_cap=cap)
        proposal = router.propose(txn)
        requested = sum(ATTEMPTS_CONSUMED[a] for a in proposal.proposed_plan)
        assert requested <= txn.attempts_left


# ---------------------------------------------------------------------------
# degradation
# ---------------------------------------------------------------------------


def test_unparseable_output_degrades_to_abstaining(tmp_path: Path) -> None:
    """A broken model must cost nothing, not act on a guess."""
    provider = CachingProvider(BrokenProvider(), ResponseCache(tmp_path / "cache"))
    router = Router(provider, SignalIndex([make_txn()]))

    proposal = router.propose(make_txn())

    assert proposal.proposed_plan == ()
    assert proposal.parse_failed is True
    assert proposal.p_recover_if_act == 0.0
    assert router.stats.parse_failures == 1
    assert router.stats.abstain_proposals == 1
    assert provider.stats.parse_failures == 1


def test_an_action_outside_the_allowed_set_degrades_to_abstaining(tmp_path: Path) -> None:
    """A hallucinated action invalidates the whole response, not just that item."""
    rogue = MockProvider(
        responder=lambda facts: {
            "root_cause": "insufficient_funds",
            "root_cause_confidence": 0.9,
            "issuer_assessment": "fine",
            "liquidity_assessment": "fine",
            "proposed_plan": ["retry_now", "call_the_customers_mother"],
            "rationale": "root_cause signal says funds",
            "p_recover_if_act": 0.7,
            "p_recover_if_abstain": 0.2,
            "draft_customer_message": None,
        }
    )
    router = Router(
        CachingProvider(rogue, ResponseCache(tmp_path / "cache")), SignalIndex([make_txn()])
    )
    proposal = router.propose(make_txn())

    assert proposal.proposed_plan == ()
    assert router.stats.schema_violations == 1
    assert proposal.parse_failed is True


def test_an_out_of_range_probability_degrades_to_abstaining(tmp_path: Path) -> None:
    rogue = MockProvider(
        responder=lambda facts: {
            "root_cause": "insufficient_funds",
            "root_cause_confidence": 0.9,
            "issuer_assessment": "fine",
            "liquidity_assessment": "fine",
            "proposed_plan": ["retry_in_2h"],
            "rationale": "root_cause signal",
            "p_recover_if_act": 7.5,
            "p_recover_if_abstain": 0.2,
            "draft_customer_message": None,
        }
    )
    router = Router(
        CachingProvider(rogue, ResponseCache(tmp_path / "cache")), SignalIndex([make_txn()])
    )
    assert router.propose(make_txn()).proposed_plan == ()
    assert router.stats.schema_violations == 1


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------


class _CountingProvider:
    model = "counting"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, schema: dict) -> dict:
        self.calls += 1
        return {
            "root_cause": "technical_error",
            "root_cause_confidence": 0.5,
            "issuer_assessment": "x",
            "liquidity_assessment": "y",
            "proposed_plan": [],
            "rationale": "root_cause signal",
            "p_recover_if_act": 0.0,
            "p_recover_if_abstain": 0.3,
            "draft_customer_message": None,
        }


def test_a_cache_hit_never_reaches_the_provider(tmp_path: Path) -> None:
    """The whole reproducibility story rests on this."""
    inner = _CountingProvider()
    provider = CachingProvider(inner, ResponseCache(tmp_path / "cache"))

    first = provider.complete("the same prompt", RESPONSE_SCHEMA)
    assert inner.calls == 1
    second = provider.complete("the same prompt", RESPONSE_SCHEMA)

    assert second == first
    assert inner.calls == 1, "a cache hit went to the provider"
    assert provider.stats.network_calls == 1
    assert provider.cache.stats.hits == 1
    assert provider.cache.stats.misses == 1

    provider.complete("a different prompt", RESPONSE_SCHEMA)
    assert inner.calls == 2


def test_cache_key_covers_both_model_and_prompt() -> None:
    assert cache_key("a", "p") != cache_key("b", "p")
    assert cache_key("a", "p") != cache_key("a", "q")
    assert cache_key("a", "p") == cache_key("a", "p")


def test_cache_survives_a_corrupt_entry(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path / "cache")
    cache.put("m", "p", {"ok": True})
    cache.path_for("m", "p").write_text("{not json", encoding="utf-8")
    assert cache.get("m", "p") is None  # a miss, not a crash


def test_no_secret_is_written_to_the_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Key material must never reach disk."""
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-value-do-not-persist")
    cache = ResponseCache(tmp_path / "cache")
    provider = CachingProvider(MockProvider(), cache)
    router = Router(provider, SignalIndex([make_txn()]))
    router.propose(make_txn())

    for path in (tmp_path / "cache").glob("*.json"):
        assert "super-secret-value-do-not-persist" not in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# provider setup
# ---------------------------------------------------------------------------


def test_gemini_provider_refuses_to_guess_a_model_name() -> None:
    """No pin, no start. A remembered model id is a silent reproducibility bug."""
    from retry_economist.llm.config import pinned_model
    from retry_economist.llm.provider import GeminiProvider

    with pytest.raises(ProviderUnavailable) as excinfo:
        GeminiProvider()
    message = str(excinfo.value)
    if pinned_model() is None:
        assert "discover" in message
    else:
        assert "GEMINI_API_KEY" in message


def test_prompt_carries_the_facts_signals_budget_and_allowed_actions() -> None:
    txn = make_txn(retry_attempts_used=1, retry_cap=3)
    signals = SignalIndex([txn]).signals_for(txn)
    prompt = build_prompt(txn, signals)

    assert "<FACTS>" in prompt and "</FACTS>" in prompt
    facts = json.loads(prompt.split("<FACTS>")[1].split("</FACTS>")[0])
    assert facts["gateway_message"] == txn.gateway_message
    assert facts["attempts_left"] == 2
    assert set(facts["signals"]) == {"root_cause", "issuer_health_now", "liquidity_timing"}
    assert "do_nothing" not in facts["allowed_actions"]
    assert "retry_now" in facts["allowed_actions"]


def test_mock_provider_is_deterministic() -> None:
    txn = make_txn()
    index = SignalIndex([txn])
    a = Router(MockProvider(), index).propose(txn)
    b = Router(MockProvider(), index).propose(txn)
    assert a.to_dict() == b.to_dict()


# ---------------------------------------------------------------------------
# the ablation policy
# ---------------------------------------------------------------------------


def test_ablation_adopts_the_proposal_verbatim(router: Router) -> None:
    """No economics, no vetoes - that is what makes it an ablation."""
    policy = LLMRouterOnlyPolicy(router)
    txn = make_txn()
    proposal = router.propose(txn)

    decision = policy.decide(txn)
    assert isinstance(decision, Decision)
    assert list(decision.plan) == list(proposal.proposed_plan)
    assert decision.reason == proposal.rationale
    # The estimates travel with the decision so the economist can use them later.
    assert decision.metadata["p_recover_if_act"] == proposal.p_recover_if_act
    assert decision.metadata["p_recover_if_abstain"] == proposal.p_recover_if_abstain


def test_ablation_records_proposals_for_calibration(router: Router) -> None:
    policy = LLMRouterOnlyPolicy(router)
    for i in range(3):
        policy.decide(make_txn(txn_id=f"p{i}"))
    assert set(policy.proposals) == {"p0", "p1", "p2"}


def test_llm_and_router_packages_pass_the_leakage_guard() -> None:
    """Neither package may reach the counterfactual store."""
    from test_eval import _leakage_findings

    for package in ("llm", "router"):
        directory = SRC / "retry_economist" / package
        assert directory.exists()
        for path in sorted(directory.rglob("*.py")):
            assert not _leakage_findings(path), f"{path.name} leaks: {_leakage_findings(path)}"


def test_everything_here_ran_without_an_api_key() -> None:
    assert os.environ.get("GEMINI_API_KEY") is None


# ---------------------------------------------------------------------------
# rate limits and resumability
# ---------------------------------------------------------------------------


class _RateLimitError(RuntimeError):
    """Shaped like the SDK's 429, so detection is tested on the real signal."""

    def __init__(self) -> None:
        super().__init__("429 RESOURCE_EXHAUSTED: quota exceeded for this model")
        self.code = 429


class _FlakyClient:
    """Rate-limits the first `fail_times` calls, then answers."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.attempts = 0

    class _Models:
        def __init__(self, outer: "_FlakyClient") -> None:
            self.outer = outer

        def generate_content(self, **kwargs):
            self.outer.attempts += 1
            if self.outer.attempts <= self.outer.fail_times:
                raise _RateLimitError()
            return type("R", (), {"text": json.dumps({
                "root_cause": "technical_error",
                "root_cause_confidence": 0.6,
                "issuer_assessment": "ok",
                "liquidity_assessment": "ok",
                "proposed_plan": ["retry_in_2h"],
                "rationale": "root_cause signal reads technical_error",
                "p_recover_if_act": 0.6,
                "p_recover_if_abstain": 0.2,
                "draft_customer_message": None,
            })})()

    @property
    def models(self):
        return self._Models(self)


def _gemini_with(client, sleeps: list[float], **kwargs):
    from retry_economist.llm.provider import GeminiProvider

    return GeminiProvider(
        model="test-model", client=client, sleep=sleeps.append, **kwargs
    )


def test_rate_limits_are_detected_on_their_signal_not_one_exception_class() -> None:
    from retry_economist.llm.provider import is_rate_limit

    assert is_rate_limit(_RateLimitError())
    assert is_rate_limit(RuntimeError("429 Too Many Requests"))
    assert is_rate_limit(RuntimeError("RESOURCE_EXHAUSTED"))
    assert is_rate_limit(RuntimeError("You exceeded your quota"))
    assert not is_rate_limit(RuntimeError("500 internal error"))
    assert not is_rate_limit(ValueError("bad json"))


def test_a_503_overload_is_treated_as_a_rate_limit_not_a_parse_failure() -> None:
    """Found in production: the free tier returns 503 "high demand" far more
    often than 429 - see docs/PROGRESS.md's Phase 4 rate-limit diagnosis. A
    503 must back off and retry the same way a 429 does, never fall through to
    the generic-API-error path, which would count it as a parse failure and
    degrade to a FABRICATED abstain proposal for what was really an
    infrastructure outage, not a bad model answer.
    """
    from retry_economist.llm.provider import is_rate_limit

    class _Overloaded(RuntimeError):
        code = 503

    assert is_rate_limit(_Overloaded("UNAVAILABLE"))
    assert is_rate_limit(RuntimeError("503 UNAVAILABLE"))
    assert is_rate_limit(
        RuntimeError(
            "This model is currently experiencing high demand. "
            "Spikes in demand are usually temporary. Please try again later."
        )
    )
    # A generic 500 must still NOT be swept into this category - it is a
    # genuine fault, not a transient capacity signal to back off and retry.
    assert not is_rate_limit(RuntimeError("500 internal error"))


def test_backoff_retries_exponentially_and_then_succeeds() -> None:
    """Three 429s, then an answer: 1s, 2s, 4s of backoff and no failure."""
    sleeps: list[float] = []
    provider = _gemini_with(_FlakyClient(fail_times=3), sleeps)

    result = provider.complete("prompt", RESPONSE_SCHEMA)

    assert result["proposed_plan"] == ["retry_in_2h"]
    assert sleeps == [1.0, 2.0, 4.0], f"expected exponential backoff, got {sleeps}"
    assert provider.stats.rate_limit_retries == 3


def test_exhausted_backoff_aborts_rather_than_abstaining() -> None:
    """Quota exhaustion is infrastructure, not a model answer.

    Degrading it to an abstain proposal would fill a scoreboard with rows that
    read "the router chose to do nothing" when the truth is "we never asked".
    That would be a fabricated result, so the run has to stop.
    """
    from retry_economist.llm.provider import RateLimited

    sleeps: list[float] = []
    provider = _gemini_with(_FlakyClient(fail_times=99), sleeps, max_rate_limit_retries=3)

    with pytest.raises(RateLimited, match="re-run to resume"):
        provider.complete("prompt", RESPONSE_SCHEMA)
    assert sleeps == [1.0, 2.0, 4.0]

    # And it must NOT be mistaken for a parse failure anywhere upstream.
    assert not issubclass(RateLimited, ParseFailure)


def test_a_rate_limited_run_is_resumable_without_losing_completed_calls(
    tmp_path: Path,
) -> None:
    """The explicit resumability check: nothing already answered is re-fetched.

    Simulates a run that dies partway through on quota, then re-runs against the
    same cache and asserts that only the unanswered transactions reach the
    provider.
    """
    from retry_economist.llm.provider import RateLimited

    txns = [make_txn(txn_id=f"r{i}", customer_id=f"c{i % 4}") for i in range(10)]
    cache = ResponseCache(tmp_path / "cache")
    budget = {"remaining": 4}

    class _QuotaLimitedProvider:
        model = "test-model"

        def __init__(self) -> None:
            self.stats = type("S", (), {"repairs": 0, "rate_limit_retries": 0})()
            self.answered = 0

        def complete(self, prompt: str, schema: dict) -> dict:
            if budget["remaining"] <= 0:
                raise RateLimited("out of quota")
            budget["remaining"] -= 1
            self.answered += 1
            return {
                "root_cause": "technical_error",
                "root_cause_confidence": 0.5,
                "issuer_assessment": "ok",
                "liquidity_assessment": "ok",
                "proposed_plan": [],
                "rationale": "root_cause signal",
                "p_recover_if_act": 0.0,
                "p_recover_if_abstain": 0.3,
                "draft_customer_message": None,
            }

    # --- first run: dies partway through --------------------------------------
    first_inner = _QuotaLimitedProvider()
    first = Router(CachingProvider(first_inner, cache), SignalIndex(txns))
    completed = 0
    with pytest.raises(RateLimited):
        for txn in txns:
            first.propose(txn)
            completed += 1

    assert completed == 4, "expected the run to stop when quota ran out"
    assert len(cache) == 4, "completed calls must be on disk"

    # --- second run: same cache, quota restored -------------------------------
    budget["remaining"] = 100
    second_inner = _QuotaLimitedProvider()
    resumed_cache = ResponseCache(tmp_path / "cache")
    second = Router(CachingProvider(second_inner, resumed_cache), SignalIndex(txns))
    proposals = [second.propose(txn) for txn in txns]

    assert len(proposals) == 10
    assert resumed_cache.stats.hits == 4, "the four completed calls were re-fetched"
    assert second_inner.answered == 6, f"expected 6 new calls, made {second_inner.answered}"
    assert len(cache) == 10
    # And none of them degraded to a fabricated abstention.
    assert second.stats.parse_failures == 0
