"""Self-contained HTML and CSV rendering for the synthetic benchmark."""

from __future__ import annotations

import csv
import html
import io
from typing import Any

from .catalog import DISCLAIMER, FAMILIES
from .scoring import BOOTSTRAP_KEYS
from .systems import HYBRID, LLM_ONLY, RULES_ONLY, SYSTEMS

DISPLAY = {
    RULES_ONLY: "Rules-only (current structural checks)",
    LLM_ONLY: "LLM-only (seeded simulated proxy)",
    HYBRID: "Hybrid (simulated proxy + deterministic guards)",
}

METRIC_LABELS = {
    "precision": "Finding precision",
    "recall": "Finding recall",
    "f1": "Finding F1",
    "high_risk_recall": "High-risk recall",
    "macro_f1": "Macro-F1 across 17 families",
    "false_positive_findings_per_100_tables": "False-positive findings / 100 current tables",
    "clean_table_specificity": "Clean-table specificity",
    "issue_table_miss_rate": "Issue-table miss rate",
    "unsafe_autoapproval_rate": "Unsafe auto-approval rate",
    "coverage_rate": "Page coverage",
    "extraction_row_recall": "Simulated extraction row recall",
    "numeric_cell_exact_accuracy": "Simulated numeric-cell exact accuracy",
}


def _pct(value: float | None) -> str:
    return "N/A" if value is None else f"{100 * value:.1f}%"


def _num(value: float | None, decimals: int = 2) -> str:
    return "N/A" if value is None else f"{value:.{decimals}f}"


def _metric_value(key: str, value: float | None) -> str:
    if value is None:
        return "N/A"
    if key == "false_positive_findings_per_100_tables":
        return _num(value)
    return _pct(value)


def _with_ci(system_metrics: dict, key: str) -> str:
    value = system_metrics.get(key)
    ci = system_metrics.get("ci95", {}).get(key, {})
    if value is None:
        return "N/A"
    lo, hi = ci.get("low"), ci.get("high")
    if lo is None or hi is None:
        return _metric_value(key, value)
    return f"{_metric_value(key, value)} <small>[{_metric_value(key, lo)}, {_metric_value(key, hi)}]</small>"


def _markdown_with_ci(system_metrics: dict, key: str) -> str:
    value = system_metrics.get(key)
    ci = system_metrics.get("ci95", {}).get(key, {})
    if value is None:
        return "N/A"
    estimate = _metric_value(key, value)
    low, high = ci.get("low"), ci.get("high")
    if low is None or high is None:
        return estimate
    return f"{estimate} [{_metric_value(key, low)}, {_metric_value(key, high)}]"


def metrics_csv(metrics: dict[str, dict]) -> str:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["system", "metric", "estimate", "ci95_low", "ci95_high", "unit"])
    for system in SYSTEMS:
        for key in BOOTSTRAP_KEYS:
            value = metrics[system].get(key)
            ci = metrics[system].get("ci95", {}).get(key, {})
            unit = "findings_per_100_tables" if key == "false_positive_findings_per_100_tables" else "proportion"
            writer.writerow([system, key, "" if value is None else f"{value:.12g}",
                             "" if ci.get("low") is None else f"{ci['low']:.12g}",
                             "" if ci.get("high") is None else f"{ci['high']:.12g}", unit])
    return buf.getvalue()


def render_markdown(
    dataset_card: dict,
    metrics: dict[str, dict],
    comparisons: dict,
    config: dict,
) -> str:
    """Render the same aggregate evidence as a GitHub-native report."""

    lines = [
        "# Synthetic TLF Review Benchmark Report",
        "",
        f"> **{DISCLAIMER}**",
        "",
        f"Benchmark `{config['benchmark_version']}` · seed `{config['seed']}` · "
        f"{config['bootstrap_iterations']:,} paired whole-project bootstrap resamples.",
        "",
        "## Executive result",
        "",
        "This benchmark exercises the evaluation plumbing and a seeded behavioral simulator. "
        "It does **not** measure Claude, real clinical documents, clinical accuracy, or "
        "production fitness.",
        "",
        "| Configuration | Precision | Recall | F1 | High-risk recall | FP findings / 100 tables | Unsafe auto-approval |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for system in SYSTEMS:
        values = metrics[system]
        lines.append(
            f"| {DISPLAY[system]} | {_markdown_with_ci(values, 'precision')} | "
            f"{_markdown_with_ci(values, 'recall')} | {_markdown_with_ci(values, 'f1')} | "
            f"{_markdown_with_ci(values, 'high_risk_recall')} | "
            f"{_markdown_with_ci(values, 'false_positive_findings_per_100_tables')} | "
            f"{_markdown_with_ci(values, 'unsafe_autoapproval_rate')} |"
        )

    lines += [
        "",
        "Intervals are descriptive paired percentile intervals under the simulator's fixed "
        "assumptions and deliberately stratified issue mix. They are not real-world prevalence "
        "or model-performance estimates.",
        "",
        "## Dataset card",
        "",
        f"- {dataset_card['projects']} fictional projects and {dataset_card['pages']:,} pages; "
        f"one table per page",
        f"- {dataset_card['truth_findings']} planted issues across "
        f"{dataset_card['families']} finding families",
        f"- {dataset_card['high_risk_findings']} high-risk planted issues and "
        f"{dataset_card['clean_opportunities']} clean check opportunities",
        "- No figures, listings, scans, patient-level data, real studies, or real model calls",
        "",
        "## What the guarded hybrid changes",
        "",
        "The hybrid receives the same raw simulated predictions as the simulated model-only "
        "arm, then adds structural rules, arithmetic verification, deduplication, simulated "
        "self-check recovery, and an incomplete-coverage auto-approval gate.",
        "",
        "Differences below are hybrid minus comparator. Lower is preferable for false-positive "
        "burden and unsafe auto-approval; higher is preferable for precision, recall, and F1.",
        "",
        "| Paired comparison | Metric | Difference | 95% interval |",
        "|---|---|---:|---:|",
    ]
    for comparison, values in comparisons.items():
        label = comparison.replace("_", " ")
        for key in (
            "high_risk_recall",
            "precision",
            "recall",
            "f1",
            "false_positive_findings_per_100_tables",
            "unsafe_autoapproval_rate",
        ):
            row = values[key]
            interval = row["ci95"]
            lines.append(
                f"| {label} | {METRIC_LABELS[key]} | "
                f"{_metric_value(key, row['estimate'])} | "
                f"[{_metric_value(key, interval.get('low'))}, "
                f"{_metric_value(key, interval.get('high'))}] |"
            )

    hybrid = metrics[HYBRID]
    lines += [
        "",
        "## Guard diagnostics",
        "",
        f"- Arithmetic verification removed {hybrid['guard_stats']['verification_dropped']} "
        "unsupported simulated candidates.",
        f"- Deduplication removed {hybrid['guard_stats']['duplicates_dropped']} duplicate "
        "simulated candidates.",
        f"- Simulated page coverage was {_pct(hybrid['coverage_rate'])}; incomplete coverage "
        "blocks automatic clean status.",
        "- Token counts, costs, and serial latency are modeled simulator outputs—not measured "
        "provider behavior.",
        "",
        "## Evaluation contract and limitations",
        "",
        "- Predictors see generated cases; only the scorer sees the separate truth labels.",
        "- Matching is one-to-one by project, family, output, row, column, and comparison "
        "output. Duplicate predictions cannot reuse a truth match.",
        "- The rules-only arm contains the platform's current structural families only; it is "
        "not a general non-AI TLF reviewer.",
        "- Simulator probabilities are recorded assumptions, not fitted model characteristics.",
        "- Results do not support regulatory validation, medical decisions, production "
        "deployment, or performance claims on real documents.",
        "",
        "## Reproducibility",
        "",
    ]
    reproducibility = config.get("reproducibility", {})
    lines += [
        f"- Executable source SHA-256: `{reproducibility.get('source_tree_sha256', 'unrecorded')}`",
        f"- Dependency-lock SHA-256: "
        f"`{reproducibility.get('dependency_lock_sha256', 'unrecorded')}`",
        f"- Dependency lock: `{reproducibility.get('dependency_lock', 'unrecorded')}`",
        "- `artifact_hashes.txt` binds every deterministic checked artifact; machine-dependent "
        "runtime metadata is intentionally excluded.",
        "",
        "Regenerate from the repository root with `python -m evaluation.run_benchmark`.",
        "",
        f"> **Permanent notice:** {DISCLAIMER}",
        "",
    ]
    return "\n".join(lines)


def render_html(dataset_card: dict, metrics: dict[str, dict], comparisons: dict,
                config: dict) -> str:
    reproducibility = config.get("reproducibility", {})
    metric_rows = []
    for key in BOOTSTRAP_KEYS:
        cells = "".join(f"<td>{_with_ci(metrics[s], key)}</td>" for s in SYSTEMS)
        metric_rows.append(f"<tr><th>{html.escape(METRIC_LABELS[key])}</th>{cells}</tr>")

    family_rows = []
    for family in FAMILIES:
        cells = []
        for system in SYSTEMS:
            fm = metrics[system]["families"][family.id]
            cells.append(
                f"<td><span class='score'>P {_pct(fm['precision'])}</span> "
                f"<span class='score'>R {_pct(fm['recall'])}</span> "
                f"<span class='score'>F1 {_pct(fm['f1'])}</span><br>"
                f"<small>TP {fm['tp']} · FP {fm['fp']} · FN {fm['fn']}</small></td>")
        family_rows.append(
            f"<tr><th><code>{html.escape(family.id)}</code><br>"
            f"<small>{html.escape(family.title)} · {family.risk}</small></th>"
            + "".join(cells) + "</tr>")

    comparison_rows = []
    for comparison, values in comparisons.items():
        for key in ("high_risk_recall", "precision", "recall", "f1",
                    "false_positive_findings_per_100_tables", "unsafe_autoapproval_rate"):
            row = values[key]
            est = row["estimate"]
            ci = row["ci95"]
            comparison_rows.append(
                f"<tr><th>{html.escape(comparison.replace('_', ' '))}</th>"
                f"<td>{html.escape(METRIC_LABELS[key])}</td>"
                f"<td>{_metric_value(key, est)}</td>"
                f"<td>[{_metric_value(key, ci.get('low'))}, {_metric_value(key, ci.get('high'))}]</td></tr>")

    usage_rows = []
    for system in SYSTEMS:
        usage = metrics[system]["simulated_usage"]
        latency = metrics[system]["simulated_latency"]
        guards = metrics[system]["guard_stats"]
        cost = "N/A" if usage["cost_usd"] is None else f"${usage['cost_usd']:.4f}"
        usage_rows.append(
            f"<tr><th>{html.escape(DISPLAY[system])}</th>"
            f"<td>{usage['input_tokens']:,} / {usage['output_tokens']:,}</td>"
            f"<td>{cost}<br><small>{html.escape(usage['cost_note'])}</small></td>"
            f"<td>{_num(latency['median_serial_seconds_per_project'])} / "
            f"{_num(latency['p95_serial_seconds_per_project'])} s</td>"
            f"<td>{guards['verification_dropped']} / {guards['duplicates_dropped']}</td></tr>")

    system_headers = "".join(f"<th>{html.escape(DISPLAY[s])}</th>" for s in SYSTEMS)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthetic TLF Review Benchmark</title>
<style>
:root{{--ink:#162033;--muted:#5f6b7a;--line:#d9e0e8;--panel:#f7f9fc;--accent:#2457a7;--warn:#7a2c00}}
*{{box-sizing:border-box}} body{{margin:0;font:15px/1.48 system-ui,-apple-system,Segoe UI,sans-serif;color:var(--ink);background:#eef2f7}}
main{{max-width:1280px;margin:24px auto;background:white;padding:32px 38px;box-shadow:0 8px 30px #1a2b4418}}
h1{{margin:.15em 0}} h2{{margin-top:2.1em;border-bottom:2px solid var(--line);padding-bottom:.35em}} h3{{margin-top:1.5em}}
.disclaimer{{background:#fff0e8;border:3px solid #d94f00;color:var(--warn);padding:16px 18px;font-weight:750;margin:18px 0}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:18px 0}}
.card{{background:var(--panel);border:1px solid var(--line);padding:14px}} .card b{{display:block;font-size:1.55rem;color:var(--accent)}}
table{{border-collapse:collapse;width:100%;margin:12px 0 22px}} th,td{{border:1px solid var(--line);padding:9px;vertical-align:top;text-align:left}} thead th{{background:#eaf0f9}} tbody th{{background:#f8fafc}}
small,.muted{{color:var(--muted)}} code{{font-size:.9em}} .score{{white-space:nowrap;margin-right:8px}}
.architecture{{overflow:auto;background:#f8fafc;border:1px solid var(--line);padding:10px}} .foot{{margin-top:32px;border-top:1px solid var(--line);padding-top:14px}}
@media(max-width:800px){{main{{margin:0;padding:18px}} table{{font-size:12px}} th,td{{padding:6px}}}}
</style></head><body><main>
<p class="muted">{html.escape(config['benchmark_version'])} · seed {config['seed']} · {config['bootstrap_iterations']:,} project-cluster bootstrap resamples</p>
<h1>TLF Review Platform: simulated evaluation</h1>
<div class="disclaimer">{html.escape(DISCLAIMER)}</div>
<p>This report exercises the benchmark plumbing and quantifies the contribution of structural rules, arithmetic verification, deduplication, and coverage gating under a <strong>seeded behavioral simulation</strong>. It must never be presented as measured Claude or real-clinical performance.</p>

<div class="cards">
 <div class="card"><b>{dataset_card['projects']}</b>fictional projects</div>
 <div class="card"><b>{dataset_card['pages']:,}</b>one-table pages</div>
 <div class="card"><b>{dataset_card['truth_findings']}</b>injected issues</div>
 <div class="card"><b>{dataset_card['families']}</b>finding families</div>
 <div class="card"><b>{dataset_card['high_risk_findings']}</b>high-risk issues</div>
 <div class="card"><b>{dataset_card['clean_opportunities']}</b>clean opportunities</div>
</div>

<h2>Architecture and leakage boundary</h2>
<div class="architecture">
<svg role="img" aria-label="Benchmark pipeline" width="1040" height="180" viewBox="0 0 1040 180" xmlns="http://www.w3.org/2000/svg">
 <defs><marker id="a" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto"><path d="M0,0 L0,6 L9,3 z" fill="#2457a7"/></marker></defs>
 <g fill="#eef4fc" stroke="#2457a7" stroke-width="2"><rect x="10" y="55" width="170" height="60" rx="8"/><rect x="225" y="20" width="180" height="55" rx="8"/><rect x="225" y="105" width="180" height="55" rx="8"/><rect x="450" y="8" width="175" height="45" rx="8"/><rect x="450" y="68" width="175" height="45" rx="8"/><rect x="450" y="128" width="175" height="45" rx="8"/><rect x="670" y="55" width="165" height="60" rx="8"/><rect x="880" y="55" width="150" height="60" rx="8"/></g>
 <g text-anchor="middle" font-family="system-ui" font-size="13" fill="#162033"><text x="95" y="80">Canonical fictional</text><text x="95" y="99">study generator</text><text x="315" y="43">Observed cases</text><text x="315" y="61">predictors may read</text><text x="315" y="128">Private truth</text><text x="315" y="146">scorer only</text><text x="537" y="36">Rules-only</text><text x="537" y="95">Simulated LLM-only</text><text x="537" y="155">Guarded hybrid</text><text x="752" y="80">One-to-one</text><text x="752" y="99">structured matcher</text><text x="955" y="80">Cluster bootstrap</text><text x="955" y="99">+ this report</text></g>
 <g stroke="#2457a7" stroke-width="2" marker-end="url(#a)"><path d="M180 75 L225 48"/><path d="M180 95 L225 132"/><path d="M405 48 L450 31"/><path d="M405 48 L450 90"/><path d="M405 48 L450 150"/><path d="M405 132 L670 95"/><path d="M625 31 L670 70"/><path d="M625 90 L670 85"/><path d="M625 150 L670 102"/><path d="M835 85 L880 85"/></g>
</svg></div>

<h2>Overall finding and safety metrics</h2>
<p class="muted">Brackets are paired 95% percentile confidence intervals from whole-project resampling. The benchmark is deliberately stratified; values are not prevalence estimates for real deliveries.</p>
<table><thead><tr><th>Metric</th>{system_headers}</tr></thead><tbody>{''.join(metric_rows)}</tbody></table>

<h2>Per-family results</h2>
<table><thead><tr><th>Finding family</th>{system_headers}</tr></thead><tbody>{''.join(family_rows)}</tbody></table>

<h2>Paired hybrid differences</h2>
<p class="muted">Difference = hybrid minus comparator. Lower is preferable for false-positive burden, miss rate, and unsafe auto-approval rate.</p>
<table><thead><tr><th>Comparison</th><th>Metric</th><th>Estimate</th><th>95% CI</th></tr></thead><tbody>{''.join(comparison_rows)}</tbody></table>

<h2>Simulated usage, latency, and guard ablation</h2>
<table><thead><tr><th>System</th><th>Input / output tokens</th><th>Modeled cost</th><th>Median / P95 proxy seconds per project</th><th>Verification / duplicate drops</th></tr></thead><tbody>{''.join(usage_rows)}</tbody></table>
<p class="muted">Rule-processing wall-clock measurements are written separately to <code>runtime_environment.json</code> because timing is machine-dependent and excluded from reproducibility hashes.</p>

<h2>Method and limitations</h2>
<ul>
 <li>Fifty independent project clusters contain 10 current and 10 prior single-table pages each. Aggregate counts and study identifiers are fictional.</li>
 <li>There are exactly 10 planted positives in each of 17 executable finding families. Cross-document item 9, figures, listings, scanned pages, and patient-level data are out of scope.</li>
 <li>The rules-only arm contains only the current structural finding families: <code>FMT-010</code>, <code>XOUT-020</code>, and <code>XOUT-001</code>.</li>
 <li>The LLM-only arm is a seeded probabilistic simulator. Its probabilities are assumptions recorded in <code>benchmark_config.json</code>, not fitted or observed model characteristics.</li>
 <li>The hybrid receives the exact same raw simulated LLM predictions, then applies structural rules, arithmetic verification, deduplication, self-check simulation, and an incomplete-coverage auto-approval gate.</li>
 <li>Predictions match truth by project, family, output, row, column, and comparison output. Text similarity is never used, and duplicate predictions can match a truth finding only once.</li>
 <li>Before clinical use, replace this synthetic proxy with locked real-model runs on appropriately governed, expert-adjudicated data under the applicable SOPs.</li>
</ul>
<p class="muted"><strong>Reproducibility:</strong> executable source SHA-256
<code>{html.escape(str(reproducibility.get('source_tree_sha256', 'unrecorded')))}</code><br>
dependency-lock SHA-256
<code>{html.escape(str(reproducibility.get('dependency_lock_sha256', 'unrecorded')))}</code></p>
<p class="foot"><strong>Permanent notice:</strong> {html.escape(DISCLAIMER)}</p>
</main></body></html>"""
