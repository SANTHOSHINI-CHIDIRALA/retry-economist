"""Guards on the demo board, so it can never drift from the committed results.

A slide with hand-copied numbers goes stale the moment anything is re-run, and
the drift is silent, one-directional and always flattering. These tests make
that impossible: the page is generated, it regenerates byte-identically, and
every statistic on it traces back to a file in the repository.

The second test is the important one. It extracts every percentage the page
renders and requires each to be either a figure the builder logged with its
source, or a literal string present in one of the committed artefacts. Nothing
can appear on the board that is not in a committed file.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "build_demo_html.py"
DEMO = REPO_ROOT / "results" / "demo.html"
SOURCES = REPO_ROOT / "results" / "demo_sources.json"

#: Everything the board is allowed to quote from.
SOURCE_FILES = (
    REPO_ROOT / "results" / "holdout_scoreboard.json",
    REPO_ROOT / "results" / "subsample_scoreboard.json",
    REPO_ROOT / "results" / "veto_precision_naive_plan.md",
    REPO_ROOT / "results" / "veto_demo_real.txt",
    REPO_ROOT / "README.md",
)

PERCENT = re.compile(r"\d+(?:\.\d+)?%")
NUM_SPAN = re.compile(r'<span class="num"[^>]*>([^<]+)</span>')


@pytest.fixture(scope="module")
def built() -> str:
    """Build the page once, from the committed inputs."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return DEMO.read_text(encoding="utf-8")


def test_the_page_regenerates_byte_identically(built: str) -> None:
    """Same inputs, same bytes - so a rebuild is never a silent edit."""
    first = DEMO.read_bytes()
    first_sources = SOURCES.read_bytes()

    result = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr

    assert DEMO.read_bytes() == first, "demo.html is not deterministic"
    assert SOURCES.read_bytes() == first_sources, "demo_sources.json is not deterministic"
    # And no timestamp crept in, which is the usual way this breaks.
    assert not re.search(r"\b20\d\d-\d\d-\d\dT\d\d:", built.replace("2026-", "SIMDATE-"))


def test_every_rendered_percentage_traces_to_a_committed_file(built: str) -> None:
    """Nothing on the board may be hand-typed.

    A percentage is acceptable if the builder logged it with a source, or if it
    appears verbatim in one of the committed artefacts - which covers text
    quoted directly, such as the headline sentence and the README limitations.
    """
    body = re.sub(r"<style>.*?</style>", "", built, flags=re.S)
    logged = {entry["rendered"] for entry in json.loads(SOURCES.read_text(encoding="utf-8"))["entries"]}
    corpus = "\n".join(path.read_text(encoding="utf-8") for path in SOURCE_FILES)

    unsourced = [
        token
        for token in PERCENT.findall(body)
        if token not in logged and token not in corpus
    ]
    assert not unsourced, f"percentages with no committed source: {sorted(set(unsourced))}"


def test_every_logged_figure_is_actually_on_the_page(built: str) -> None:
    """The provenance log must describe the page, not a page that once existed."""
    on_page = NUM_SPAN.findall(built)
    logged = [entry["rendered"] for entry in json.loads(SOURCES.read_text(encoding="utf-8"))["entries"]]

    assert sorted(on_page) == sorted(logged)
    assert len(logged) > 50, "suspiciously few sourced figures"
    for entry in json.loads(SOURCES.read_text(encoding="utf-8"))["entries"]:
        assert entry["source"], entry
        assert entry["path"], entry


def test_headline_and_key_figures_match_the_scoreboard(built: str) -> None:
    """Spot-check the numbers a viewer will actually read off the screen."""
    board = json.loads(
        (REPO_ROOT / "results" / "holdout_scoreboard.json").read_text(encoding="utf-8")
    )
    assert board["headline"] in built
    assert str(board["n_transactions"]) in built
    assert str(board["n_customers"]) in built

    by_name = {p["name"]: p for p in board["policies"]}
    for name in ("rules_only", "retry_economist (prior)", "naive_retry_3x"):
        rate = by_name[name]["metrics"]["recovery_rate"]
        assert f"{rate * 100:.1f}%" in built, f"{name} recovery rate missing"

    # The cheating bound must be on the page AND flagged as one.
    assert "oracle_best (CHEATS)" in built
    assert "upper bound, not a result" in built


def test_the_page_is_self_contained(built: str) -> None:
    """It will be screen-recorded offline: nothing may be fetched at view time."""
    assert "<script" not in built.lower(), "no scripts - the page must be inert"
    for marker in ("http://", "https://", "cdn.", "mermaid"):
        assert marker not in built.lower(), f"external reference found: {marker}"
    assert "<style>" in built, "styles must be inlined"


def test_wide_tables_scroll_rather_than_wrap(built: str) -> None:
    """On a phone, a wrapped table is unreadable; it must scroll in place."""
    assert built.count('<div class="scroll">') >= 3
    assert "overflow-x: auto" in built
    assert "white-space: nowrap" in built


def test_the_limitations_section_names_the_subsample(built: str) -> None:
    """The board must not let the LLM rows read as a full-holdout result."""
    subsample = json.loads(
        (REPO_ROOT / "results" / "subsample_scoreboard.json").read_text(encoding="utf-8")
    )
    assert "What this cannot do" in built
    assert str(subsample["n_transactions"]) in built
    assert "directional only" in built


# ---------------------------------------------------------------------------
# the paired-comparison verdict column
# ---------------------------------------------------------------------------


def _verdict():
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from build_demo_html import paired_verdict

    return paired_verdict


def test_verdict_states_direction_for_every_case() -> None:
    """All three branches, including the one no current row exercises.

    Every paired comparison in the committed data is currently significant, so
    the "not significant" path would otherwise ship untested.
    """
    paired_verdict = _verdict()

    loss = paired_verdict(
        {"uplift_significant": True, "net_uplift_pp_delta": {"point": -2.54}, "subject": "A"}
    )
    win = paired_verdict(
        {"uplift_significant": True, "net_uplift_pp_delta": {"point": 8.95}, "subject": "A"}
    )
    null = paired_verdict(
        {"uplift_significant": False, "net_uplift_pp_delta": {"point": -2.54}, "subject": "A"}
    )

    assert "LESS" in loss and "MORE" not in loss
    assert "MORE" in win and "LESS" not in win
    assert "not significant" in null
    assert "LESS" not in null and "MORE" not in null
    # The word "supported" must not come back - it was the ambiguity.
    for rendered in (loss, win, null):
        assert "supported" not in rendered


def test_the_page_never_labels_a_loss_as_a_win(built: str) -> None:
    """A negative delta must carry LESS on the page, positive must carry MORE."""
    board = json.loads(
        (REPO_ROOT / "results" / "holdout_scoreboard.json").read_text(encoding="utf-8")
    )
    paired_verdict = _verdict()

    for comparison in board["paired_comparisons"]:
        rendered = paired_verdict(comparison)
        assert rendered in built, f"verdict missing for {comparison['subject']}"
        if comparison["uplift_significant"] and comparison["net_uplift_pp_delta"]["point"] < 0:
            assert "LESS" in rendered, comparison["subject"]

    # And the old ambiguous wording is gone from the whole page.
    assert ">supported<" not in built
    assert "straddles zero" not in built


def test_the_trade_off_line_matches_the_scoreboard(built: str) -> None:
    """The sentence under the table must be the table's own numbers."""
    board = json.loads(
        (REPO_ROOT / "results" / "holdout_scoreboard.json").read_text(encoding="utf-8")
    )
    by_name = {p["name"]: p["metrics"] for p in board["policies"]}
    econ, rules = by_name["retry_economist (prior)"], by_name["rules_only"]
    delta = next(
        c
        for c in board["paired_comparisons"]
        if c["subject"] == "retry_economist (prior)" and c["baseline"] == "rules_only"
    )["net_uplift_pp_delta"]["point"]

    assert f"{abs(delta):.1f}pp" in built
    assert f"{econ['total_attempts']:,}" in built
    assert f"{rules['total_attempts']:,}" in built
    for metrics in (econ, rules):
        spend = metrics["total_cost_rupees"] + metrics["annoyance_cost_rupees"]
        assert f"{spend:,.0f}" in built, f"spend {spend} missing"
    # It must read as a loss, not a win.
    assert "recovers <span class=\"num\"" in built or "recovers " in built
    assert "<strong>less</strong> than" in built
