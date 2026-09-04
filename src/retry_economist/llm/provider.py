"""The provider protocol, a real Gemini client, and a deterministic stand-in.

Two rules shape this module.

Garbage degrades to inaction, never to action. A model that returns unparseable
output gets exactly one repair attempt, and then the caller is handed a
`ParseFailure`. The router turns that into an ABSTAIN proposal and counts it.
The alternative - guessing at a plan from a broken response - would let a
malfunctioning model spend a merchant's money, which is the one failure mode
this system must not have.

The cache sits in front of everything, so a cache hit never opens a socket. That
is what lets the whole evaluation replay offline from the committed cache.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from retry_economist.llm.cache import DEFAULT_CACHE_DIR, ResponseCache
from retry_economist.llm.config import API_KEY_ENV, pinned_model


class ParseFailure(RuntimeError):
    """The model did not return usable structured output, twice."""


class RateLimited(RuntimeError):
    """The provider kept rate-limiting us after the backoff budget ran out.

    Deliberately NOT a `ParseFailure`. Unparseable output is a model problem and
    degrades to abstaining, which costs nothing. Exhausted quota is an
    INFRASTRUCTURE problem, and degrading it to abstention would quietly fill a
    scoreboard with "the router chose to do nothing" rows that are really "we
    never asked". That would look like a result and be a fabrication, so it
    aborts the run instead. The cache makes the re-run cheap.
    """


class ProviderUnavailable(RuntimeError):
    """The provider cannot start - no key, or no pinned model."""


@runtime_checkable
class LLMProvider(Protocol):
    """Anything that can answer a prompt with a structured JSON object."""

    model: str

    def complete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class ProviderStats:
    calls: int = 0
    network_calls: int = 0
    repairs: int = 0
    parse_failures: int = 0
    rate_limit_retries: int = 0
    total_latency_seconds: float = 0.0

    @property
    def mean_latency_seconds(self) -> float | None:
        return self.total_latency_seconds / self.calls if self.calls else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "network_calls": self.network_calls,
            "repairs": self.repairs,
            "parse_failures": self.parse_failures,
            "rate_limit_retries": self.rate_limit_retries,
            "mean_latency_seconds": (
                None
                if self.mean_latency_seconds is None
                else round(self.mean_latency_seconds, 4)
            ),
            "total_latency_seconds": round(self.total_latency_seconds, 3),
        }


#: Backoff schedule for 429s (and 503s - see below): 1s, 2s, 4s, 8s, 16s, then
#: give up and abort. Doubling rather than a fixed sleep because a quota
#: window - or a capacity shortage - that is busy now is likely still busy a
#: second later, and hammering it makes the queue worse.
DEFAULT_RATE_LIMIT_RETRIES = 5
DEFAULT_INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 32.0

#: Rate limits surface differently depending on transport and SDK version, so
#: this matches on the signal rather than on one exception class.
_RATE_LIMIT_MARKERS = (
    "429",
    "resource_exhausted",
    "resourceexhausted",
    "rate limit",
    "ratelimit",
    "quota",
    "too many requests",
)

#: 503 is NOT a quota rejection - it is the provider's shared capacity being
#: overloaded ("high demand", "please try again later"). Found in practice on
#: the free tier: the SDK's own default internal retry (5 attempts, its own
#: exponential backoff) usually absorbs it silently, but when THAT budget is
#: exhausted the exception reaches this code - and without these markers it
#: would fall through to the "genuine API error" branch in `_attempt`, count
#: as a parse failure, and degrade to a FABRICATED abstain proposal. That
#: would misrepresent "the provider was down when we asked" as "the model
#: produced nothing usable", which is exactly the kind of result this module's
#: docstring says a malfunctioning model must never be allowed to look like.
#: A 503 gets the same backoff-then-`RateLimited` treatment as a 429 instead;
#: kept as separate markers from `_RATE_LIMIT_MARKERS` so a bare "500 internal
#: error" (a genuine, non-transient fault) still does NOT match either set.
_OVERLOAD_MARKERS = (
    "503",
    "unavailable",
    "overloaded",
    "high demand",
    "try again later",
)


def is_rate_limit(exc: BaseException) -> bool:
    """Whether an exception is the provider asking us to slow down and retry -
    either because we are asking too fast (429/quota) or because its shared
    capacity is temporarily overloaded (503). Both back off identically;
    see `_OVERLOAD_MARKERS` for why 503 belongs here rather than in the
    generic-API-error path.
    """
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in (429, 503):
        return True
    haystack = f"{type(exc).__name__} {exc}".lower()
    return any(marker in haystack for marker in _RATE_LIMIT_MARKERS) or any(
        marker in haystack for marker in _OVERLOAD_MARKERS
    )


_REPAIR_PREAMBLE = (
    "Your previous response could not be parsed as JSON matching the required "
    "schema. Return ONLY a single valid JSON object matching the schema exactly. "
    "No prose, no markdown fences, no trailing commentary.\n\n"
)


# ---------------------------------------------------------------------------
# caching wrapper
# ---------------------------------------------------------------------------


@dataclass
class CachingProvider:
    """Wraps any provider with the on-disk cache.

    Deliberately a wrapper rather than a mixin, so the cache cannot be bypassed
    by a provider that forgets to call it: whatever the inner provider is, the
    only way to reach it is through here.
    """

    inner: LLMProvider
    cache: ResponseCache = field(default_factory=lambda: ResponseCache(DEFAULT_CACHE_DIR))
    stats: ProviderStats = field(default_factory=ProviderStats)

    @property
    def model(self) -> str:
        return self.inner.model

    def complete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.stats.calls += 1
        started = time.perf_counter()

        cached = self.cache.get(self.model, prompt)
        if cached is not None:
            self.stats.total_latency_seconds += time.perf_counter() - started
            return cached

        # Miss: this is the only path that may touch the network.
        self.stats.network_calls += 1
        try:
            response = self.inner.complete(prompt, schema)
        except ParseFailure:
            self.stats.parse_failures += 1
            self.stats.total_latency_seconds += time.perf_counter() - started
            raise
        except RateLimited:
            # Nothing is cached for this prompt, so a re-run retries exactly
            # this call and keeps every completed one.
            self.stats.total_latency_seconds += time.perf_counter() - started
            raise
        inner_stats = getattr(self.inner, "stats", None)
        if inner_stats is not None:
            self.stats.repairs = inner_stats.repairs
            self.stats.rate_limit_retries = inner_stats.rate_limit_retries

        self.cache.put(self.model, prompt, response)
        self.stats.total_latency_seconds += time.perf_counter() - started
        return response

    def report(self) -> dict[str, Any]:
        return {"model": self.model, **self.stats.to_dict(), "cache": self.cache.stats.to_dict()}


# ---------------------------------------------------------------------------
# real provider
# ---------------------------------------------------------------------------


class GeminiProvider:
    """Google Gemini via the `google-genai` package, in structured-output mode.

    The model id is never hardcoded; it comes from `model_pin.json`, which
    `python -m retry_economist.llm.discover` writes after listing what the API
    actually offers. Starting without a pin is an error with instructions, not a
    guess at a plausible-looking name.
    """

    def __init__(
        self,
        model: str | None = None,
        *,
        api_key: str | None = None,
        client: Any = None,
        max_rate_limit_retries: int = DEFAULT_RATE_LIMIT_RETRIES,
        initial_backoff_seconds: float = DEFAULT_INITIAL_BACKOFF_SECONDS,
        min_request_interval_seconds: float = 0.0,
        sleep: Any = None,
    ) -> None:
        resolved = model or pinned_model()
        if resolved is None:
            raise ProviderUnavailable(
                "no model pinned. Run `python -m retry_economist.llm.discover` once with "
                f"{API_KEY_ENV} set; it lists the available models, pins the cheapest "
                "flash-tier one supporting structured output, and commits the choice."
            )
        self.model = resolved
        self.max_rate_limit_retries = max_rate_limit_retries
        self.initial_backoff_seconds = initial_backoff_seconds
        #: Reactive backoff alone is not enough against a per-minute quota: a
        #: burst that lands inside one window can exhaust the retry budget before
        #: the window rolls over. This paces requests proactively so the run
        #: mostly never sees a 429 in the first place. Zero by default (and left
        #: zero in every test) so it never perturbs the exact backoff-sleep
        #: sequences those tests assert on; the CLI turns it on for real runs.
        self.min_request_interval_seconds = min_request_interval_seconds
        self._last_request_monotonic: float | None = None
        # Injectable so the backoff path is testable without a key or a network.
        self._sleep = sleep or time.sleep
        self.stats = ProviderStats()

        if client is not None:
            self._client = client
            return

        key = api_key or os.environ.get(API_KEY_ENV)
        if not key:
            raise ProviderUnavailable(
                f"{API_KEY_ENV} is not set. Every cached prompt replays without it; "
                "only new prompts need a key."
            )

        try:
            from google import genai  # imported lazily so the package stays optional
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise ProviderUnavailable("google-genai is not installed") from exc

        self._client = genai.Client(api_key=key)

    def complete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        parsed = self._attempt(prompt, schema)
        if parsed is not None:
            return parsed

        # One repair attempt. Not a loop: a model that cannot produce valid JSON
        # twice will not produce it on the fifth try either, and each retry costs
        # real money and real latency.
        self.stats.repairs += 1
        parsed = self._attempt(_REPAIR_PREAMBLE + prompt, schema)
        if parsed is not None:
            return parsed

        self.stats.parse_failures += 1
        raise ParseFailure("model returned unparseable output twice")

    def _attempt(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any] | None:
        """One generation, with backoff on rate limits. None means unparseable."""
        text = self._generate_with_backoff(prompt, schema)
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _generate_with_backoff(self, prompt: str, schema: dict[str, Any]) -> str | None:
        """Call the API, backing off exponentially while it returns 429.

        A rate limit is not a failed answer, so it is never treated as one:
        after the budget is exhausted this raises `RateLimited` and the run
        aborts. Everything already answered is in the cache, so re-running
        resumes rather than starting over.
        """
        delay = self.initial_backoff_seconds
        for attempt in range(self.max_rate_limit_retries + 1):
            self._pace()
            try:
                response = self._client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config={
                        "response_mime_type": "application/json",
                        "response_schema": schema,
                        # Deterministic as far as the API allows, so a re-run
                        # against a cold cache lands close to the committed one.
                        "temperature": 0.0,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                if not is_rate_limit(exc):
                    # A genuine API or transport error is a failed attempt; the
                    # repair pass gets one more go before we abstain.
                    return None
                if attempt >= self.max_rate_limit_retries:
                    raise RateLimited(
                        f"rate limited after {self.max_rate_limit_retries} backoff attempts; "
                        "re-run to resume - completed calls are already cached"
                    ) from exc
                self.stats.rate_limit_retries += 1
                self._sleep(delay)
                delay = min(delay * 2, MAX_BACKOFF_SECONDS)
                continue
            return (getattr(response, "text", None) or "").strip()
        return None

    def _pace(self) -> None:
        """Sleep just enough to keep requests at least this far apart."""
        if self.min_request_interval_seconds <= 0:
            return
        now = time.monotonic()
        if self._last_request_monotonic is not None:
            wait = self.min_request_interval_seconds - (now - self._last_request_monotonic)
            if wait > 0:
                self._sleep(wait)
        self._last_request_monotonic = time.monotonic()


# ---------------------------------------------------------------------------
# deterministic stand-in
# ---------------------------------------------------------------------------


class MockProvider:
    """Deterministic provider used by every test, and by offline runs.

    It is NOT a language model and nothing here should be reported as one. It
    reads the same facts block the real prompt carries and applies a fixed
    heuristic, so the router, the cache, the ablation policy and the calibration
    machinery can all be exercised and asserted without a network or a key.

    Its purpose is to make the architecture testable offline. Any result
    produced with this provider must be labelled with the provider that produced
    it - the scoreboard does that in its header.
    """

    model = "mock-deterministic-v1"

    def __init__(self, responder: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> None:
        self._responder = responder
        self.stats = ProviderStats()

    def complete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        facts = _extract_facts(prompt)
        if facts is None:
            self.stats.parse_failures += 1
            raise ParseFailure("mock provider could not find a facts block in the prompt")
        if self._responder is not None:
            return self._responder(facts)
        from retry_economist.llm.heuristic import heuristic_proposal

        return heuristic_proposal(facts)


FACTS_OPEN = "<FACTS>"
FACTS_CLOSE = "</FACTS>"


def _extract_facts(prompt: str) -> dict[str, Any] | None:
    """Pull the machine-readable facts block back out of a rendered prompt."""
    start = prompt.find(FACTS_OPEN)
    end = prompt.find(FACTS_CLOSE)
    if start < 0 or end < 0 or end < start:
        return None
    try:
        return json.loads(prompt[start + len(FACTS_OPEN) : end])
    except json.JSONDecodeError:
        return None


class BrokenProvider:
    """Always unparseable. Exists so the degradation path is tested, not assumed."""

    model = "broken-test-provider"

    def __init__(self) -> None:
        self.stats = ProviderStats()

    def complete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.stats.repairs += 1
        self.stats.parse_failures += 1
        raise ParseFailure("deliberately unparseable")
