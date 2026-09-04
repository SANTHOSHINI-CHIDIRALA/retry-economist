"""The pinned model id, and why it is not written in this file by hand.

Model names drift: they get renamed, deprecated, and silently repointed at new
weights. A name typed in from memory is a name that may not exist, may not be
the cheapest option, or may quietly become a different model than the one an
experiment was run against - at which point the results stop being reproducible
and nobody notices.

So the id is DISCOVERED, not remembered. `python -m retry_economist.llm.discover`
lists what the API actually offers, filters to the flash tier with structured
JSON output, picks the cheapest, and writes the exact string to `model_pin.json`
next to this file. That file is committed, so every later run is pinned to the
model the numbers were produced with.

Until that command has been run with a working key, `pinned_model()` returns
None and `GeminiProvider` refuses to start with an instruction rather than
guessing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: Written by `python -m retry_economist.llm.discover`. Committed deliberately.
PIN_PATH = Path(__file__).with_name("model_pin.json")

#: Environment variable the key is read from. Never written to disk or logged.
API_KEY_ENV = "GEMINI_API_KEY"


def load_pin() -> dict[str, Any] | None:
    """The full pin record, or None if discovery has never been run."""
    if not PIN_PATH.exists():
        return None
    try:
        return json.loads(PIN_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def pinned_model() -> str | None:
    """The exact model id this project is pinned to, or None."""
    pin = load_pin()
    return None if pin is None else pin.get("model")


def write_pin(model: str, *, considered: list[dict[str, Any]], note: str) -> Path:
    """Record the discovered model and what it was chosen over.

    The rejected candidates are stored too: "we picked the cheapest flash model"
    is only checkable if the list it was cheapest among is on record.
    """
    PIN_PATH.write_text(
        json.dumps(
            {"model": model, "note": note, "considered": considered}, indent=2, sort_keys=True
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return PIN_PATH
