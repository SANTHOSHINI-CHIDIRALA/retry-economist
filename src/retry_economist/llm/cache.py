"""On-disk response cache, and the reproducibility property it buys.

Every provider call goes through here. The key is a hash of the model id and the
exact prompt, so a hit is only ever a hit for the same question asked of the same
model - and a hit never touches the network.

The cache directory is COMMITTED. That is the point of it: with the cache in the
repository, the full evaluation replays offline, with no API key, on any machine,
and produces the same numbers a reviewer can check. An LLM result that cannot be
re-derived is an anecdote; this is what makes it an artefact.

Nothing secret is stored. The key material is the model id and the prompt, and
the prompt is built from the observed feed - never from credentials.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CACHE_DIR = Path("data/llm_cache")


def cache_key(model: str, prompt: str) -> str:
    """sha256 over model and prompt. Changing either is a different question."""
    digest = hashlib.sha256()
    digest.update(model.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(prompt.encode("utf-8"))
    return digest.hexdigest()


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    writes: int = 0

    @property
    def lookups(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float | None:
        return self.hits / self.lookups if self.lookups else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "lookups": self.lookups,
            "hit_rate": None if self.hit_rate is None else round(self.hit_rate, 6),
        }


@dataclass
class ResponseCache:
    """A flat directory of JSON files, one per (model, prompt) pair."""

    directory: Path = DEFAULT_CACHE_DIR
    stats: CacheStats = field(default_factory=CacheStats)

    def path_for(self, model: str, prompt: str) -> Path:
        return self.directory / f"{cache_key(model, prompt)}.json"

    def get(self, model: str, prompt: str) -> dict[str, Any] | None:
        path = self.path_for(model, prompt)
        if not path.exists():
            self.stats.misses += 1
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # A corrupt entry is a miss, not a crash: the run refills it.
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return payload["response"]

    def put(self, model: str, prompt: str, response: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        # The prompt is stored alongside the response so a reviewer can see
        # exactly what was asked, not just what came back.
        self.path_for(model, prompt).write_text(
            json.dumps(
                {"model": model, "prompt": prompt, "response": response},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        self.stats.writes += 1

    def __len__(self) -> int:
        return len(list(self.directory.glob("*.json"))) if self.directory.exists() else 0
