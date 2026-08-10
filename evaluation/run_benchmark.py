"""Generate, score, and report the fully offline simulated TLF benchmark.

Run from the project root:

    python -m evaluation.run_benchmark
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import platform
import sys
import time
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # Permit ``python evaluation/run_benchmark.py``.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.catalog import (BENCHMARK_SEED, BENCHMARK_VERSION, DISCLAIMER,
                                FAMILIES, SIMULATOR_VERSION, taxonomy_json)
from evaluation.generate import generate_dataset, validate_dataset
from evaluation.report import DISPLAY, metrics_csv, render_html, render_markdown
from evaluation.scoring import score_all
from evaluation.systems import (DETECTION_PROBABILITY, DIFFICULTY_MULTIPLIER,
                                FALSE_POSITIVE_PROBABILITY, HYBRID, LLM_ONLY,
                                RULES_ONLY, SYSTEMS, run_systems,
                                structural_rules)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "artifacts"
PUBLIC_REPORT = Path(__file__).resolve().parent / "REPORT.md"
BENCHMARK_SOURCE_FILES = (
    "evaluation/catalog.py",
    "evaluation/generate.py",
    "evaluation/report.py",
    "evaluation/run_benchmark.py",
    "evaluation/scoring.py",
    "evaluation/systems.py",
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reproducibility_record(root: Path = ROOT) -> dict[str, Any]:
    """Bind checked benchmark artifacts to executable source and the exact lock."""

    digest = hashlib.sha256()
    for relative in BENCHMARK_SOURCE_FILES:
        raw = (root / relative).read_bytes()
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return {
        "source_tree_sha256": digest.hexdigest(),
        "dependency_lock": "requirements-lock.txt",
        "dependency_lock_sha256": _sha256_file(root / "requirements-lock.txt"),
        "source_files": list(BENCHMARK_SOURCE_FILES),
    }


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _jsonl_text(rows: list[dict]) -> str:
    return "".join(json.dumps(row, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")) + "\n" for row in rows)


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def _dataset_card(cases: list[dict], truth: list[dict]) -> dict:
    by_family = {f.id: 0 for f in FAMILIES}
    by_risk = {"High": 0, "Low": 0}
    for item in truth:
        by_family[item["family"]] += 1
        by_risk[item["risk"]] += 1
    opportunities = sum(len(c["opportunities"]) for c in cases)
    return {
        "name": "Synthetic one-table-per-page TLF stress benchmark",
        "benchmark_version": BENCHMARK_VERSION,
        "disclaimer": DISCLAIMER,
        "projects": len(cases),
        "current_pages": sum(1 for c in cases for p in c["pages"] if p["document_role"] == "current"),
        "prior_pages": sum(1 for c in cases for p in c["pages"] if p["document_role"] == "prior"),
        "pages": sum(len(c["pages"]) for c in cases),
        "tables_per_page": 1,
        "figures": 0,
        "listings": 0,
        "families": len(FAMILIES),
        "opportunities": opportunities,
        "truth_findings": len(truth),
        "clean_opportunities": opportunities - len(truth),
        "high_risk_findings": by_risk["High"],
        "low_risk_findings": by_risk["Low"],
        "findings_by_family": by_family,
        "scope_exclusions": [
            "real clinical or patient data", "cross-document checklist item 9",
            "figures", "listings", "scanned/image-only pages",
            "actual LLM performance", "regulatory validation",
        ],
    }


def _per_project_csv(records: dict[str, list[dict]]) -> str:
    buf = io.StringIO(newline="")
    fields = [
        "system", "project_id", "tp", "fp", "fn", "high_tp", "high_fn",
        "current_tables", "clean_tables", "true_negative_tables",
        "issue_bearing_tables", "issue_tables_predicted_clean",
        "autoapproved_tables", "unsafe_autoapproved_tables", "pages_total",
        "pages_read", "rows_total", "rows_extracted", "numeric_cells_evaluated",
        "numeric_cells_exact", "cells_recovered_by_self_check",
        "input_tokens_simulated", "output_tokens_simulated",
        "latency_seconds_simulated_serial",
    ]
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for system in SYSTEMS:
        for row in records[system]:
            writer.writerow({"system": system, **row})
    return buf.getvalue()


def _hash_artifacts(output_dir: Path, names: list[str]) -> str:
    lines = [
        "# SHA-256 hashes for deterministic benchmark artifacts",
        "# runtime_environment.json is intentionally excluded because wall-clock timing and platform metadata vary.",
    ]
    for name in sorted(names):
        digest = hashlib.sha256((output_dir / name).read_bytes()).hexdigest()
        lines.append(f"{digest}  {name}")
    return "\n".join(lines) + "\n"


def run(output_dir: Path, *, n_projects: int = 50, positives_per_family: int = 10,
        seed: int = BENCHMARK_SEED, bootstrap_iterations: int = 2000,
        input_price: float | None = None,
        output_price: float | None = None) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    t0 = time.perf_counter()
    cases, truth = generate_dataset(n_projects=n_projects,
                                    positives_per_family=positives_per_family, seed=seed)
    validate_dataset(cases, truth, n_projects=n_projects,
                     positives_per_family=positives_per_family)
    generation_seconds = time.perf_counter() - t0

    # Machine-dependent measurement of the actual local rules baseline. It lives
    # only in runtime_environment.json and never contaminates deterministic hashes.
    t0 = time.perf_counter()
    for case in cases:
        structural_rules(case, seed)
    measured_rules_seconds = time.perf_counter() - t0

    t0 = time.perf_counter()
    outputs = run_systems(cases, seed)
    systems_seconds = time.perf_counter() - t0
    t0 = time.perf_counter()
    metrics, comparisons, detail = score_all(
        cases, truth, outputs, bootstrap_iterations=bootstrap_iterations, seed=seed,
        input_price=input_price, output_price=output_price)
    scoring_seconds = time.perf_counter() - t0

    card = _dataset_card(cases, truth)
    config = {
        "benchmark_version": BENCHMARK_VERSION,
        "simulator_version": SIMULATOR_VERSION,
        "disclaimer": DISCLAIMER,
        "seed": seed,
        "projects": n_projects,
        "pages_per_project": 20,
        "current_tables_per_project": 10,
        "prior_tables_per_project": 10,
        "one_table_per_page": True,
        "families": len(FAMILIES),
        "positives_per_family": positives_per_family,
        "expected_total_findings": positives_per_family * len(FAMILIES),
        "bootstrap_iterations": bootstrap_iterations,
        "bootstrap_unit": "project",
        "matching_key": ["project_id", "family", "output_label", "row", "column",
                         "comparison_output"],
        "simulator_assumptions": {
            "detection_probability": DETECTION_PROBABILITY,
            "false_positive_probability": FALSE_POSITIVE_PROBABILITY,
            "difficulty_multiplier": DIFFICULTY_MULTIPLIER,
            "duplicate_probability": 0.09,
            "numeric_citation_corruption_probability": 0.04,
            "self_check_recovery_probability": 0.82,
        },
        "token_prices_per_million": {"input": input_price, "output": output_price},
        "reproducibility": reproducibility_record(),
    }
    result = {
        "disclaimer": DISCLAIMER,
        "benchmark_version": BENCHMARK_VERSION,
        "dataset": card,
        "systems": {s: {"display_name": DISPLAY[s], **metrics[s]} for s in SYSTEMS},
        "paired_comparisons": comparisons,
    }

    deterministic: dict[str, str] = {
        "benchmark_config.json": _json_text(config),
        "dataset_card.json": _json_text(card),
        "taxonomy.json": _json_text(taxonomy_json()),
        "cases.jsonl": _jsonl_text(cases),
        "truth.jsonl": _jsonl_text(truth),
        "metrics.json": _json_text(result),
        "paired_comparisons.json": _json_text(comparisons),
        "metrics.csv": metrics_csv(metrics),
        "per_project_metrics.csv": _per_project_csv(detail["records"]),
        "REPORT.md": render_markdown(card, metrics, comparisons, config),
        "report.html": render_html(card, metrics, comparisons, config),
    }
    for system in SYSTEMS:
        deterministic[f"predictions_{system}.jsonl"] = _jsonl_text(outputs[system]["predictions"])
        # Matching decisions provide an auditable TP/FP/FN trail without relying on prose.
        deterministic[f"matching_{system}.json"] = _json_text(detail["matches"][system])
    for name, text in deterministic.items():
        _write(output_dir / name, text)
    _write(output_dir / "artifact_hashes.txt",
           _hash_artifacts(output_dir, list(deterministic)))

    total_seconds = time.perf_counter() - started
    runtime = {
        "disclaimer": DISCLAIMER,
        "reproducibility_note": (
            "Machine-dependent runtime metadata; deliberately excluded from artifact_hashes.txt. "
            "All other listed artifacts are deterministic for the same configuration."
        ),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "generation_seconds_measured": generation_seconds,
        "rules_only_detection_seconds_measured": measured_rules_seconds,
        "rules_only_microseconds_per_project_measured": measured_rules_seconds * 1_000_000 / n_projects,
        "rules_only_microseconds_per_current_table_measured": measured_rules_seconds * 1_000_000 / (n_projects * 10),
        "all_systems_seconds_measured": systems_seconds,
        "scoring_and_bootstrap_seconds_measured": scoring_seconds,
        "total_seconds_measured": total_seconds,
    }
    _write(output_dir / "runtime_environment.json", _json_text(runtime))
    return {"result": result, "runtime": runtime, "output_dir": str(output_dir)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path,
                        default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--seed", type=int, default=BENCHMARK_SEED)
    parser.add_argument("--projects", type=int, default=50)
    parser.add_argument("--positives-per-family", type=int, default=10)
    parser.add_argument("--bootstrap", type=int, default=2000)
    parser.add_argument("--input-price-per-million", type=float, default=None)
    parser.add_argument("--output-price-per-million", type=float, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if (args.input_price_per_million is None) != (args.output_price_per_million is None):
        raise SystemExit("provide both input and output token prices, or neither")
    payload = run(
        args.output_dir, n_projects=args.projects,
        positives_per_family=args.positives_per_family, seed=args.seed,
        bootstrap_iterations=args.bootstrap,
        input_price=args.input_price_per_million,
        output_price=args.output_price_per_million,
    )
    reference_run = (
        args.output_dir.resolve() == DEFAULT_OUTPUT_DIR.resolve()
        and args.seed == BENCHMARK_SEED
        and args.projects == 50
        and args.positives_per_family == 10
        and args.bootstrap == 2000
        and args.input_price_per_million is None
        and args.output_price_per_million is None
    )
    if reference_run:
        _write(PUBLIC_REPORT, (DEFAULT_OUTPUT_DIR / "REPORT.md").read_text(encoding="utf-8"))
    systems = payload["result"]["systems"]
    print(DISCLAIMER)
    print(f"Artifacts: {payload['output_dir']}")
    print(f"Dataset: {payload['result']['dataset']['projects']} projects / "
          f"{payload['result']['dataset']['pages']} pages / "
          f"{payload['result']['dataset']['truth_findings']} injected findings")
    for system in SYSTEMS:
        m = systems[system]
        print(f"{system}: precision={m['precision']:.3f} recall={m['recall']:.3f} "
              f"F1={m['f1']:.3f} high-risk-recall={m['high_risk_recall']:.3f}")
    print(f"Completed in {payload['runtime']['total_seconds_measured']:.3f}s "
          f"({args.bootstrap} project-cluster bootstrap iterations).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
