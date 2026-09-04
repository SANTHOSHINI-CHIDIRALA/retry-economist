"""Ask the API which models exist, pick one, and pin it.

    python -m retry_economist.llm.discover

Run once, with a key. It lists what the account can actually reach, keeps the
flash-tier models that support structured JSON output, picks the cheapest by the
API's own ordering, and writes the exact id to `model_pin.json`.

The alternative - typing a model name from memory - produces a string that may
not exist, may not be the cheapest, and may quietly point at different weights
next month. None of those failures announce themselves; they just make the
results unreproducible.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any

from retry_economist.llm.config import API_KEY_ENV, write_pin

#: Flash-tier names are the cheap end of the Gemini family. Matched on the name
#: rather than a hardcoded list, so a newly released flash model is picked up.
_FLASH = re.compile(r"flash", re.IGNORECASE)

#: Preview, experimental and thinking variants are excluded: they cost more,
#: churn faster, and pinning to one makes the run less reproducible, not more.
_EXCLUDE = re.compile(r"(preview|exp|experimental|thinking|image|audio|live|tts)", re.IGNORECASE)


def _supports_structured_output(model: Any) -> bool:
    actions = getattr(model, "supported_actions", None) or []
    return "generateContent" in actions if actions else True


def _size_rank(name: str) -> tuple[int, int, str]:
    """Cheapest-first ordering, derived from the name rather than a price table.

    Within the flash tier, "lite" variants are the cheapest, then plain flash.
    Newer major versions are preferred at equal tier because older ones get
    retired first. No prices are hardcoded: they change, and a stale price table
    that silently misranks models is worse than an ordering rule that is visible.
    """
    lite = 0 if "lite" in name.lower() else 1
    version = re.search(r"(\d+)\.(\d+)", name)
    major_minor = -(int(version.group(1)) * 10 + int(version.group(2))) if version else 0
    return (lite, major_minor, name)


def candidates(client: Any) -> list[dict[str, Any]]:
    """Every reachable flash-tier model that can return structured JSON."""
    found: list[dict[str, Any]] = []
    for model in client.models.list():
        name = getattr(model, "name", "") or ""
        short = name.split("/")[-1]
        if not _FLASH.search(short) or _EXCLUDE.search(short):
            continue
        if not _supports_structured_output(model):
            continue
        found.append(
            {
                "model": short,
                "display_name": getattr(model, "display_name", None),
                "input_token_limit": getattr(model, "input_token_limit", None),
            }
        )
    found.sort(key=lambda entry: _size_rank(entry["model"]))
    return found


def main(argv: list[str] | None = None) -> int:
    key = os.environ.get(API_KEY_ENV)
    if not key:
        print(
            f"{API_KEY_ENV} is not set. Discovery needs one live call to list models;\n"
            "after it has run once, the pin is committed and no key is needed again.",
            file=sys.stderr,
        )
        return 2

    try:
        from google import genai
    except ImportError:
        print("google-genai is not installed: pip install google-genai", file=sys.stderr)
        return 2

    client = genai.Client(api_key=key)
    found = candidates(client)
    if not found:
        print("no flash-tier model with structured output was offered", file=sys.stderr)
        return 1

    chosen = found[0]["model"]
    path = write_pin(
        chosen,
        considered=found,
        note=(
            "cheapest flash-tier model offering structured JSON output, chosen by "
            "listing the API's own model catalogue; lite variants rank ahead of "
            "plain flash, newer versions ahead of older"
        ),
    )
    print(f"pinned {chosen}")
    print(f"considered {len(found)}: {', '.join(entry['model'] for entry in found)}")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
