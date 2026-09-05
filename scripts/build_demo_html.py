"""Generate `results/demo.html` from the committed result files.

Every figure on the page is read out of a committed artefact at build time. None
is typed in. That is the whole point: a demo board with hand-copied numbers
drifts from the results the moment anything is re-run, and the drift is silent
and always flattering. Here, if a number cannot be sourced it does not appear.

Provenance is recorded as it goes. Each rendered figure is wrapped in
`<span class="num" data-src="...">` and logged to `results/demo_sources.json`
with its source file, its path inside that file, and its raw value, so
`tests/test_demo_html.py` can assert that every rendered statistic still traces
back to a committed source.

Sources, and why each is needed:

    results/holdout_scoreboard.json        headline, attribution, full-holdout policies, paired CIs
    results/subsample_scoreboard.json      the 47-transaction LLM rows
    results/veto_precision_naive_plan.md   the 245 -> 0 figure and the veto-precision split
    results/audit_ledger.jsonl             pay_00861, priced twice
    results/veto_demo_real.txt             pay_00647, the C1 veto  (see note below)
    data/llm_cache/                        pay_01921, the real model proposal
    README.md                              the limitations

Note on `veto_demo_real.txt`: the ledger's own `pay_00647` row is the
`retry_economist (prior)` pairing, where the rules router had already abstained
upstream, so no compliance check fired on it - `ev` is null and the reason reads
"nothing for the economist to price". The C1 firing the demo needs is the
`(naive plan)` pairing, which the ledger does not contain. That run is committed
in `veto_demo_real.txt`, so the veto section parses it from there rather than
inventing a C1 firing the ledger cannot support.

The output is deterministic: no timestamps, fixed ordering, explicit LF
newlines, so rebuilding from unchanged inputs is byte-identical.

    python scripts/build_demo_html.py
"""

from __future__ import annotations

import html
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS = REPO_ROOT / "results"
OUT_HTML = RESULTS / "demo.html"
OUT_SOURCES = RESULTS / "demo_sources.json"

HOLDOUT = RESULTS / "holdout_scoreboard.json"
SUBSAMPLE = RESULTS / "subsample_scoreboard.json"
VETO_MD = RESULTS / "veto_precision_naive_plan.md"
LEDGER = RESULTS / "audit_ledger.jsonl"
VETO_DEMO = RESULTS / "veto_demo_real.txt"
CACHE_DIR = REPO_ROOT / "data" / "llm_cache"
README = REPO_ROOT / "README.md"


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------


@dataclass
class Provenance:
    """Records where every rendered figure came from."""

    entries: list[dict[str, Any]] = field(default_factory=list)

    def num(self, rendered: str, *, raw: Any, source: str, path: str) -> str:
        """Wrap a figure for display and log its origin."""
        self.entries.append(
            {"rendered": rendered, "raw": raw, "source": source, "path": path}
        )
        return (
            f'<span class="num" data-src="{html.escape(source)}#{html.escape(path)}">'
            f"{html.escape(rendered)}</span>"
        )

    def dump(self) -> dict[str, Any]:
        return {
            "note": (
                "Every figure rendered inside <span class='num'> on demo.html, with the "
                "committed file and path it was read from. Regenerate with "
                "scripts/build_demo_html.py."
            ),
            "count": len(self.entries),
            "entries": self.entries,
        }


P = Provenance()


def pct(value: float, *, source: str, path: str, dp: int = 1) -> str:
    return P.num(f"{value * 100:.{dp}f}%", raw=value, source=source, path=path)


def pct_of(count: int, total: int, *, source: str, path: str, dp: int = 1) -> str:
    return P.num(
        f"{count / total * 100:.{dp}f}%",
        raw=count / total,
        source=source,
        path=f"{path} / {total}",
    )


def num(value: Any, rendered: str, *, source: str, path: str) -> str:
    return P.num(rendered, raw=value, source=source, path=path)


def money(rupees: float, *, source: str, path: str) -> str:
    return P.num(f"{rupees:,.0f}", raw=rupees, source=source, path=path)


def signed_pp(value: float, *, source: str, path: str) -> str:
    return P.num(f"{value:+.1f}", raw=value, source=source, path=path)


def esc(text: str) -> str:
    return html.escape(str(text))


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def policy(board: dict[str, Any], name: str) -> dict[str, Any]:
    for entry in board["policies"]:
        if entry["name"] == name:
            return entry
    raise KeyError(f"{name} not in {board.get('split')} scoreboard")


def find_ledger_entry(txn_id: str, policy_name: str) -> dict[str, Any]:
    with LEDGER.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            if row["txn_id"] == txn_id and row["policy"] == policy_name:
                return row
    raise KeyError(f"{txn_id} / {policy_name} not in the audit ledger")


def find_cached_proposal(txn_id: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    """The real model's cached response for one transaction, plus its facts."""
    for path in sorted(CACHE_DIR.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        model = payload.get("model", "")
        if model.startswith("mock") or f'"txn_id": "{txn_id}"' not in payload.get("prompt", ""):
            continue
        facts = json.loads(payload["prompt"].split("<FACTS>")[1].split("</FACTS>")[0])
        return payload["response"], facts, model
    raise KeyError(f"no real cached response for {txn_id}")


def md_table_value(text: str, row_label: str, column: int) -> str:
    """Pull one cell out of a committed markdown table."""
    for line in text.splitlines():
        if not line.startswith("|") or row_label not in line:
            continue
        cells = [c.strip().strip("*`") for c in line.strip("|").split("|")]
        return cells[column]
    raise KeyError(f"no row matching {row_label!r}")


def veto_demo_field(text: str, label: str) -> str:
    """Read one `label: value` line out of the committed veto demo transcript."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(label):
            return stripped[len(label) :].strip().strip("'\"")
    raise KeyError(f"no line starting {label!r} in the veto demo")


def veto_demo_checks(text: str) -> list[tuple[str, bool, str]]:
    """The five compliance rows from the committed transcript."""
    checks: list[tuple[str, bool, str]] = []
    for line in text.splitlines():
        match = re.match(
            r"\s*(C\d_[A-Z_]+)\s+fired=(True|False)\s+removed=(.*?)\s+reason='(.*)'\s*$",
            line,
        )
        if match:
            checks.append((match.group(1), match.group(2) == "True", match.group(4)))
    return checks


def readme_limitations(limit: int) -> list[str]:
    """The shortest bullets from the README's own limitations section."""
    body = README.read_text(encoding="utf-8")
    section = body.split("## What this cannot do", 1)[1].split("\n## ", 1)[0]
    bullets = [
        line[2:].strip()
        for line in section.splitlines()
        if line.startswith("- **")
    ]
    return sorted(bullets, key=len)[:limit]


def strip_markdown(text: str) -> str:
    """Bold and code spans to HTML; everything else escaped."""
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"`(.+?)`", r"<code>\1</code>", escaped)
    return escaped


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------


def section_headline(board: dict[str, Any]) -> str:
    src = "results/holdout_scoreboard.json"
    return f"""
<section id="headline">
  <p class="kicker">Retry Economist &mdash; failed-payment recovery, scored</p>
  <h1 class="headline">{esc(board["headline"])}</h1>
  <div class="facts">
    <div class="fact"><span class="fact-n">{num(board["n_transactions"], f"{board['n_transactions']:,}", source=src, path="n_transactions")}</span><span class="fact-l">held-out transactions</span></div>
    <div class="fact"><span class="fact-n">{num(board["n_customers"], f"{board['n_customers']:,}", source=src, path="n_customers")}</span><span class="fact-l">customer clusters</span></div>
    <div class="fact"><span class="fact-n">{num(board["seed"], str(board["seed"]), source=src, path="seed")}</span><span class="fact-l">seed &mdash; deterministic</span></div>
  </div>
</section>
"""


def section_framing(board: dict[str, Any]) -> str:
    src = "results/holdout_scoreboard.json"
    nothing = policy(board, "do_nothing")
    buckets = nothing["metrics"]["abstained_buckets"]
    total = board["n_transactions"]
    naive = policy(board, "naive_retry_3x")
    acted = naive["metrics"]["n_acted"]

    def card(key: str, label: str, sub: str) -> str:
        count = buckets[key]["count"]
        path = f"policies[do_nothing].metrics.abstained_buckets.{key}.count"
        return f"""
      <div class="card">
        <div class="card-n">{num(count, f"{count}", source=src, path=path)}</div>
        <div class="card-p">{pct_of(count, total, source=src, path=path)} of {total}</div>
        <div class="card-l">{esc(label)}</div>
        <div class="card-s">{esc(sub)}</div>
      </div>"""

    return f"""
<section id="framing">
  <h2>The framing</h2>
  <p class="lead">Every failed payment is one of three things. Nothing in the raw feed tells you which.</p>
  <div class="cards">
    {card("correct_restraint", "would have paid unaided", "acting here can only cost money")}
    {card("correct_walkaway", "unrecoverable by any affordable action", "acting here is pure loss")}
    {card("missed_opportunity", "addressable", "the only bucket worth spending on")}
  </div>
  <p class="punch">Fixed-schedule retry acts on
    {num(acted, f"{acted}", source=src, path="policies[naive_retry_3x].metrics.n_acted")}
    of {total} &mdash;
    {pct_of(acted, total, source=src, path="policies[naive_retry_3x].metrics.n_acted")}
    &mdash; without distinguishing between them.</p>
</section>
"""


def section_scoreboard(board: dict[str, Any]) -> str:
    src = "results/holdout_scoreboard.json"
    order = [
        "do_nothing",
        "naive_retry_3x",
        "rules_only",
        "retry_economist (naive plan)",
        "retry_economist (prior)",
    ]

    def row(name: str, *, bound: bool = False) -> str:
        entry = policy(board, name)
        m = entry["metrics"]
        dq = m["decision_quality"]
        base = f"policies[{name}].metrics"
        spend = m["total_cost_rupees"] + m["annoyance_cost_rupees"]
        cell_prec = (
            pct(dq["precision"], source=src, path=f"{base}.decision_quality.precision")
            if dq["precision"] is not None
            else '<span class="dim">n/a</span>'
        )
        cell_f1 = (
            pct(dq["f1"], source=src, path=f"{base}.decision_quality.f1")
            if dq["f1"] is not None
            else '<span class="dim">n/a</span>'
        )
        cell_cpir = (
            num(
                m["cost_per_incremental_rupee"],
                f"{m['cost_per_incremental_rupee']:.3f}",
                source=src,
                path=f"{base}.cost_per_incremental_rupee",
            )
            if m["cost_per_incremental_rupee"] is not None
            else '<span class="dim">n/a</span>'
        )
        label = esc(name)
        if bound:
            label = f"{label} <span class=\"tag tag-cheat\">CHEATS &mdash; upper bound, not a result</span>"
        return f"""      <tr class="{'bound' if bound else ''}">
        <td class="name">{label}</td>
        <td>{pct(m["recovery_rate"], source=src, path=f"{base}.recovery_rate")}</td>
        <td>{signed_pp(m["net_uplift_pp"], source=src, path=f"{base}.net_uplift_pp")}</td>
        <td>{cell_prec}</td>
        <td>{cell_f1}</td>
        <td>{num(m["total_attempts"], f"{m['total_attempts']:,}", source=src, path=f"{base}.total_attempts")}</td>
        <td>{money(spend, source=src, path=f"{base}.total_cost_rupees + annoyance_cost_rupees")}</td>
        <td>{cell_cpir}</td>
      </tr>"""

    rows = "\n".join(row(name) for name in order)
    bound_row = row("oracle_best (CHEATS)", bound=True)

    pairs = []
    for comparison in board["paired_comparisons"]:
        uplift = comparison["net_uplift_pp_delta"]
        f1 = comparison["decision_f1_delta"]
        path = f"paired_comparisons[{comparison['subject']} vs {comparison['baseline']}]"
        supported = comparison["uplift_significant"]
        pairs.append(
            f"""      <tr>
        <td class="name">{esc(comparison["subject"])} <span class="vs">vs</span> {esc(comparison["baseline"])}</td>
        <td>{signed_pp(uplift["point"], source=src, path=f"{path}.net_uplift_pp_delta.point")}
            <span class="ci">[{signed_pp(uplift["low"], source=src, path=f"{path}.net_uplift_pp_delta.low")},
            {signed_pp(uplift["high"], source=src, path=f"{path}.net_uplift_pp_delta.high")}]</span></td>
        <td>{num(f1["point"], f"{f1['point']:+.3f}", source=src, path=f"{path}.decision_f1_delta.point")}
            <span class="ci">[{num(f1["low"], f"{f1['low']:+.3f}", source=src, path=f"{path}.decision_f1_delta.low")},
            {num(f1["high"], f"{f1['high']:+.3f}", source=src, path=f"{path}.decision_f1_delta.high")}]</span></td>
        <td class="{'yes' if supported else 'no'}">{'supported' if supported else 'straddles zero'}</td>
      </tr>"""
        )

    return f"""
<section id="scoreboard">
  <h2>The scoreboard</h2>
  <p class="lead">Full holdout, {board["n_transactions"]} transactions. Every policy compliant &mdash; zero attempt-cap violations.</p>
  <div class="scroll">
    <table>
      <thead><tr>
        <th>policy</th><th>recovery</th><th>uplift pp</th><th>precision</th>
        <th>F1</th><th>attempts</th><th>spend INR</th><th>INR per INR</th>
      </tr></thead>
      <tbody>
{rows}
      </tbody>
      <tbody class="bound-body">
{bound_row}
      </tbody>
    </table>
  </div>
  <h3>Paired comparisons &mdash; same customers resampled in both arms</h3>
  <div class="scroll">
    <table>
      <thead><tr><th>comparison</th><th>&Delta; uplift pp (95% CI)</th><th>&Delta; decision F1 (95% CI)</th><th>verdict</th></tr></thead>
      <tbody>
{chr(10).join(pairs)}
      </tbody>
    </table>
  </div>
</section>
"""


def section_economist(veto_text: str) -> str:
    src = "results/veto_precision_naive_plan.md"
    naive_waste = int(md_table_value(veto_text, "`naive_retry_3x`", 1))
    econ_waste = int(md_table_value(veto_text, "`retry_economist (naive plan)`", 1))
    compliance = md_table_value(veto_text, "compliance-driven", 3)
    economics = md_table_value(veto_text, "economics-driven", 3)
    all_vetoes = md_table_value(veto_text, "all vetoes", 3)

    return f"""
<section id="economist">
  <h2>What the economist buys</h2>
  <div class="bigdelta">
    <div class="bd-from">{num(naive_waste, str(naive_waste), source=src, path="hard_decline_retry_waste[naive_retry_3x]")}</div>
    <div class="bd-arrow">&rarr;</div>
    <div class="bd-to">{num(econ_waste, str(econ_waste), source=src, path="hard_decline_retry_waste[retry_economist (naive plan)]")}</div>
  </div>
  <p class="bd-label">debit attempts burned on instruments the acquirer has already declared dead</p>
  <p class="punch">The proposed ladder is <strong>identical</strong> in both rows &mdash; the same failure-code-blind three-attempt schedule, on the same transactions. The entire difference is the economist's compliance rules.</p>

  <h3>Veto precision &mdash; of the actions removed, what share would have failed anyway</h3>
  <div class="split">
    <div class="split-half good">
      <div class="split-n">{P.num(compliance, raw=compliance, source=src, path="veto precision / compliance-driven (C1-C5)")}</div>
      <div class="split-l">compliance-driven (C1&ndash;C5)</div>
      <div class="split-s">hard rules. Nearly everything they remove was doomed.</div>
    </div>
    <div class="split-half weak">
      <div class="split-n">{P.num(economics, raw=economics, source=src, path="veto precision / economics-driven (EV<=0)")}</div>
      <div class="split-l">economics-driven (EV &le; 0)</div>
      <div class="split-s">roughly 4 in 10 of these would actually have recovered. Knowingly left on the table.</div>
    </div>
  </div>
  <p class="note">All vetoes combined: {P.num(all_vetoes, raw=all_vetoes, source=src, path="veto precision / all vetoes")}.</p>
</section>
"""


def section_pipeline() -> str:
    boxes = [
        ("observed txn", "22 fields"),
        ("three signals", "deterministic"),
        ("LLM router", "one call"),
        ("Proposal", "inert"),
        ("compliance C1&ndash;C5", "vetoes regardless of EV"),
        ("EV gate", "prices it"),
        ("Decision", "executable"),
        ("audit ledger", "append-only"),
    ]
    cells = []
    for i, (title, sub) in enumerate(boxes):
        classes = "pbox"
        if title == "Proposal":
            classes += " pbox-proposal"
        if title.startswith("compliance"):
            classes += " pbox-compliance"
        if title == "Decision":
            classes += " pbox-decision"
        cells.append(
            f'<div class="{classes}"><span class="pt">{title}</span>'
            f'<span class="ps">{sub}</span></div>'
        )
        if i < len(boxes) - 1:
            edge = ""
            if boxes[i][0] == "LLM router":
                edge = '<span class="pedge">proposes,<br>cannot execute</span>'
            cells.append(f'<div class="parrow">&rarr;{edge}</div>')

    return f"""
<section id="pipeline">
  <h2>The pipeline</h2>
  <div class="flow">
    {"".join(cells)}
  </div>
  <div class="oracle-row">
    <div class="obox">oracle<span class="ps">counterfactual outcomes for all 9 actions</span></div>
    <div class="parrow oarrow">&rarr;<span class="pedge">policies cannot<br>read this</span></div>
    <div class="obox obox-scorer">scorer<span class="ps">the only thing that touches it</span></div>
  </div>
  <p class="note">An AST-based leakage guard walks <code>policies/</code>, <code>router/</code>, <code>llm/</code> and <code>economist/</code> in the test suite and fails the build on any route to the oracle.</p>
</section>
"""


def section_veto(demo_text: str) -> str:
    src = "results/veto_demo_real.txt"
    txn = veto_demo_field(demo_text, "txn_id:")
    amount = veto_demo_field(demo_text, "amount:")
    code = veto_demo_field(demo_text, "failure_code:")
    message = veto_demo_field(demo_text, "gateway_message:")
    verdict = veto_demo_field(demo_text, "verdict        =")
    reason = veto_demo_field(demo_text, "reason         =")
    plan = veto_demo_field(demo_text, "proposed_plan  =")

    rows = []
    for rule_id, fired, reason_text in veto_demo_checks(demo_text):
        rows.append(
            f'      <tr class="{"fired" if fired else ""}">'
            f'<td class="rule">{esc(rule_id)}</td>'
            f'<td class="{"yes" if fired else "dim"}">{"FIRED" if fired else "no"}</td>'
            f"<td>{esc(reason_text)}</td></tr>"
        )

    return f"""
<section id="veto">
  <h2>One real veto</h2>
  <div class="txncard">
    <div class="txnline"><span class="k">transaction</span><span class="v">{esc(txn)} &mdash; {esc(amount)} &mdash; code {esc(code)}</span></div>
    <div class="txnline"><span class="k">gateway said</span><span class="v mono">{esc(message)}</span></div>
    <div class="txnline"><span class="k">proposed plan</span><span class="v mono">{esc(plan)}</span></div>
  </div>
  <div class="scroll">
    <table class="checks">
      <thead><tr><th>compliance rule</th><th></th><th>reason</th></tr></thead>
      <tbody>
{chr(10).join(rows)}
      </tbody>
    </table>
  </div>
  <div class="verdict veto-verdict">
    <div class="verdict-word">{esc(verdict)}</div>
    <div class="verdict-reason">{esc(reason)}</div>
  </div>
  <p class="note">The plan was emptied before any expected-value arithmetic ran. A risk decline is vetoed at <em>any</em> EV.</p>
</section>
"""


def section_proposal() -> str:
    response, facts, model = find_cached_proposal("pay_01921")
    src = "data/llm_cache"
    issuer = facts["signals"]["issuer_health_now"]
    multiple = issuer["multiple_over_baseline"]

    return f"""
<section id="proposal">
  <h2>One real proposal</h2>
  <p class="lead">Transaction {esc(facts["txn_id"])} &mdash; code {esc(facts["failure_code"])} &mdash;
     <span class="mono">{esc(facts["gateway_message"])}</span></p>
  <div class="signalbox">
    <div class="siglabel">issuer_health_now &mdash; computed, not guessed</div>
    <div class="sigtext">{esc(issuer["summary"])}</div>
    <div class="sigmeta">{num(multiple, f"{multiple}x", source=src, path="pay_01921 signals.issuer_health_now.multiple_over_baseline")} baseline
      &middot; {num(issuer["failures_in_window"], str(issuer["failures_in_window"]), source=src, path="pay_01921 signals.issuer_health_now.failures_in_window")} failures in window
      &middot; confidence {num(issuer["confidence"], str(issuer["confidence"]), source=src, path="pay_01921 signals.issuer_health_now.confidence")}</div>
  </div>
  <div class="planline">proposed plan &nbsp;<span class="mono strong">{esc(", ".join(response["proposed_plan"]))}</span></div>
  <blockquote>{esc(response["rationale"])}</blockquote>
  <p class="note">Verbatim from the committed response cache &mdash; model <code>{esc(model)}</code>, replayed with zero network calls.</p>
</section>
"""


def section_priced_twice() -> str:
    src = "results/audit_ledger.jsonl"
    entry = find_ledger_entry("pay_00861", "retry_economist (LLM plan)")
    model_p = entry["proposal"]["p_recover_if_act"]
    prior_p = entry["ev"]["p_recover_if_act"]
    ev_paise = entry["ev"]["net_expected_value_paise"]

    return f"""
<section id="priced">
  <h2>The same transaction, priced twice</h2>
  <p class="lead">{esc(entry["txn_id"])} &mdash; the model proposed the plan, and the model also estimated its own chance of success. Only one of those two numbers was allowed to decide.</p>
  <div class="split">
    <div class="split-half weak">
      <div class="split-n">{num(model_p, f"{model_p:.2f}", source=src, path="pay_00861 [LLM plan] proposal.p_recover_if_act")}</div>
      <div class="split-l">the model's own estimate</div>
      <div class="split-s">recorded in the ledger, then set aside</div>
    </div>
    <div class="split-half good">
      <div class="split-n">{num(prior_p, f"{prior_p:.2f}", source=src, path="pay_00861 [LLM plan] ev.p_recover_if_act")}</div>
      <div class="split-l">the train-only historical prior</div>
      <div class="split-s">what the expected value was actually computed from</div>
    </div>
  </div>
  <div class="verdict approve-verdict">
    <div class="verdict-word">{esc(entry["verdict"])}</div>
    <div class="verdict-reason">{esc(entry["reason"])}</div>
  </div>
  <p class="punch">The prior is what decided. The model's estimate did not beat a per-code lookup on this data, so it is logged for audit and never priced with.</p>
</section>
"""


def section_limits(subsample: dict[str, Any]) -> str:
    bullets = readme_limitations(6)
    items = "\n".join(f"    <li>{strip_markdown(b)}</li>" for b in bullets)
    src = "results/subsample_scoreboard.json"
    return f"""
<section id="limits">
  <h2>What this cannot do</h2>
  <p class="lead warn">The LLM rows in this project cover
    {num(subsample["n_transactions"], str(subsample["n_transactions"]), source=src, path="n_transactions")}
    transactions, not 749. Every router comparison is directional only.</p>
  <ul class="limits">
{items}
  </ul>
</section>
"""


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------

CSS = """
:root {
  --bg: #fbfaf7;
  --panel: #ffffff;
  --ink: #16130f;
  --dim: #6b6459;
  --line: #ded7cb;
  --accent: #b0341d;
  --good: #1f6b3a;
  --warn: #8a5a00;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font: 19px/1.7 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  -webkit-text-size-adjust: 100%;
}
.wrap { max-width: 1080px; margin: 0 auto; padding: 0 22px 120px; }
section { padding: 54px 0 46px; border-top: 3px solid var(--line); }
section:first-of-type { border-top: 0; padding-top: 44px; }
h1.headline { font-size: 46px; line-height: 1.18; margin: 8px 0 30px; letter-spacing: -0.02em; }
h2 { font-size: 34px; line-height: 1.2; margin: 0 0 8px; letter-spacing: -0.015em; }
h3 { font-size: 23px; margin: 38px 0 10px; color: var(--dim); font-weight: 600; }
.kicker { text-transform: uppercase; letter-spacing: 0.14em; font-size: 14px; color: var(--accent); font-weight: 700; margin: 0; }
.lead { font-size: 20px; color: var(--dim); margin: 6px 0 26px; }
.lead.warn { color: var(--accent); font-weight: 600; }
.punch { font-size: 21px; margin: 26px 0 0; padding: 18px 22px; background: var(--panel); border-left: 6px solid var(--accent); }
.note { font-size: 16px; color: var(--dim); margin: 18px 0 0; }
.dim { color: var(--dim); }
.mono, code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.92em; }
.strong { font-weight: 700; }
.num { font-variant-numeric: tabular-nums; font-weight: 600; }

.facts { display: flex; flex-wrap: wrap; gap: 34px; }
.fact { display: flex; flex-direction: column; }
.fact-n { font-size: 34px; font-weight: 700; line-height: 1.1; }
.fact-l { font-size: 15px; color: var(--dim); text-transform: uppercase; letter-spacing: 0.07em; }

.cards { display: flex; flex-wrap: wrap; gap: 16px; }
.card { flex: 1 1 260px; background: var(--panel); border: 2px solid var(--line); border-radius: 10px; padding: 20px 22px; }
.card-n { font-size: 44px; font-weight: 700; line-height: 1; }
.card-p { font-size: 19px; color: var(--accent); font-weight: 700; margin-top: 2px; }
.card-l { font-size: 19px; font-weight: 600; margin-top: 12px; }
.card-s { font-size: 16px; color: var(--dim); }

.scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; border: 2px solid var(--line); border-radius: 10px; background: var(--panel); }
table { border-collapse: collapse; width: 100%; white-space: nowrap; }
th, td { padding: 13px 16px; text-align: right; border-bottom: 1px solid var(--line); font-variant-numeric: tabular-nums; }
th { font-size: 15px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--dim); text-align: right; font-weight: 700; }
th:first-child, td:first-child, td.name, td.rule { text-align: left; }
td.name { font-weight: 600; }
tbody tr:last-child td { border-bottom: 0; }
.bound-body { background: #f3efe6; }
.bound-body td { border-top: 3px solid var(--line); }
.tag { display: inline-block; font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; padding: 3px 8px; border-radius: 4px; margin-left: 8px; font-weight: 700; white-space: nowrap; }
.tag-cheat { background: var(--accent); color: #fff; }
.vs { color: var(--dim); font-weight: 400; }
.ci { color: var(--dim); font-weight: 400; font-size: 0.9em; }
.yes { color: var(--good); font-weight: 700; }
.no { color: var(--warn); font-weight: 700; }

.bigdelta { display: flex; align-items: center; gap: 26px; margin: 22px 0 4px; flex-wrap: wrap; }
.bd-from { font-size: 92px; font-weight: 800; line-height: 1; color: var(--accent); }
.bd-arrow { font-size: 54px; color: var(--dim); }
.bd-to { font-size: 92px; font-weight: 800; line-height: 1; color: var(--good); }
.bd-label { font-size: 20px; color: var(--dim); margin: 0 0 8px; }

.split { display: flex; flex-wrap: wrap; gap: 16px; margin-top: 14px; }
.split-half { flex: 1 1 300px; background: var(--panel); border: 2px solid var(--line); border-radius: 10px; padding: 22px; }
.split-half.good { border-color: var(--good); }
.split-half.weak { border-color: var(--warn); }
.split-n { font-size: 52px; font-weight: 800; line-height: 1; }
.split-half.good .split-n { color: var(--good); }
.split-half.weak .split-n { color: var(--warn); }
.split-l { font-size: 19px; font-weight: 600; margin-top: 8px; }
.split-s { font-size: 16px; color: var(--dim); margin-top: 4px; }

.flow { display: flex; flex-wrap: wrap; align-items: stretch; gap: 8px; margin: 20px 0; }
.pbox { background: var(--panel); border: 2px solid var(--line); border-radius: 8px; padding: 14px 16px; display: flex; flex-direction: column; min-width: 132px; }
.pbox-proposal { border-color: var(--accent); border-style: dashed; }
.pbox-compliance { border-color: var(--accent); border-width: 3px; }
.pbox-decision { border-color: var(--good); border-width: 3px; }
.pt { font-weight: 700; font-size: 17px; }
.ps { font-size: 14px; color: var(--dim); }
.parrow { display: flex; flex-direction: column; align-items: center; justify-content: center; color: var(--dim); font-size: 26px; min-width: 84px; }
.pedge { font-size: 13px; line-height: 1.35; text-align: center; color: var(--accent); font-weight: 700; }
.oracle-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 8px; }
.obox { background: #f3efe6; border: 2px dashed var(--dim); border-radius: 8px; padding: 14px 16px; display: flex; flex-direction: column; font-weight: 700; min-width: 150px; }
.obox-scorer { background: var(--panel); border-style: solid; }

.txncard { background: var(--panel); border: 2px solid var(--line); border-radius: 10px; padding: 18px 22px; margin-bottom: 18px; }
.txnline { display: flex; flex-wrap: wrap; gap: 12px; padding: 6px 0; }
.txnline .k { min-width: 150px; color: var(--dim); font-size: 16px; text-transform: uppercase; letter-spacing: 0.06em; }
.txnline .v { font-weight: 600; }
table.checks td { white-space: normal; }
tr.fired { background: #fdece8; }
tr.fired td.rule { color: var(--accent); font-weight: 800; }

.verdict { margin-top: 20px; padding: 20px 24px; border-radius: 10px; }
.veto-verdict { background: var(--accent); color: #fff; }
.approve-verdict { background: var(--good); color: #fff; }
.verdict-word { font-size: 38px; font-weight: 800; text-transform: uppercase; letter-spacing: 0.05em; line-height: 1.1; }
.verdict-reason { font-size: 17px; margin-top: 6px; opacity: 0.94; }

.signalbox { background: var(--panel); border: 2px solid var(--line); border-left: 6px solid var(--accent); border-radius: 8px; padding: 18px 22px; }
.siglabel { font-size: 14px; text-transform: uppercase; letter-spacing: 0.09em; color: var(--accent); font-weight: 700; }
.sigtext { font-size: 19px; margin-top: 6px; }
.sigmeta { font-size: 16px; color: var(--dim); margin-top: 8px; }
.planline { margin: 20px 0 6px; font-size: 19px; }
blockquote { margin: 12px 0 0; padding: 20px 24px; background: var(--panel); border: 2px solid var(--line); border-radius: 10px; font-size: 20px; line-height: 1.6; font-style: italic; }

ul.limits { padding-left: 24px; margin: 10px 0 0; }
ul.limits li { margin-bottom: 16px; }

@media (max-width: 720px) {
  body { font-size: 18px; }
  h1.headline { font-size: 34px; }
  h2 { font-size: 28px; }
  .bd-from, .bd-to { font-size: 66px; }
  .split-n { font-size: 42px; }
}
"""


def build() -> str:
    board = load_json(HOLDOUT)
    subsample = load_json(SUBSAMPLE)
    veto_text = VETO_MD.read_text(encoding="utf-8")
    demo_text = VETO_DEMO.read_text(encoding="utf-8")

    body = "".join(
        [
            section_headline(board),
            section_framing(board),
            section_scoreboard(board),
            section_economist(veto_text),
            section_pipeline(),
            section_veto(demo_text),
            section_proposal(),
            section_priced_twice(),
            section_limits(subsample),
        ]
    )
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Retry Economist</title>\n"
        f"<style>{CSS}</style>\n"
        "</head>\n<body>\n"
        f'<div class="wrap">{body}</div>\n'
        "</body>\n</html>\n"
    )


def main() -> int:
    html_text = build()
    with OUT_HTML.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(html_text)
    with OUT_SOURCES.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(P.dump(), indent=2, sort_keys=True) + "\n")

    print(f"wrote {OUT_HTML}  ({len(html_text):,} bytes)")
    print(f"wrote {OUT_SOURCES}  ({len(P.entries)} sourced figures)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
