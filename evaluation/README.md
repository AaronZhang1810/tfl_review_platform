# Synthetic TLF Evaluation

> **SYNTHETIC ENGINEERING BENCHMARK.** Every study, table, count, issue, and model behavior in this package is simulated. Results are not measurements on real TLFs, are not measurements of Claude, do not constitute clinical validation, and do not establish regulatory fitness.

This package provides a reproducible, offline demonstration of how to evaluate three review configurations:

1. `rules_only` — the platform's three current deterministic structural finding families (`FMT-010`, `XOUT-020`, and `XOUT-001`).
2. `llm_only_simulated` — a seeded behavioral proxy with explicit detection, false-positive, duplication, and transcription-error assumptions.
3. `hybrid_simulated` — the exact same raw proxy outputs plus structural rules, arithmetic verification, deduplication, simulated suspect-cell correction, and incomplete-coverage gating.

The default benchmark has 50 fictional projects. Each project has ten current and ten prior pages with exactly one table per page: 1,000 pages in total. It plants exactly ten issues in each of 17 executable finding families, for 170 truth findings. Cross-document item 9, figures, listings, scanned PDFs, and patient-level data are excluded.

## Run

From the repository root:

```bash
python -m evaluation.run_benchmark
```

Artifacts are written to `evaluation/artifacts/`. The HTML report is fully self-contained and requires no server or internet connection. The same run also writes `evaluation/REPORT.md`, a GitHub-native rendering of the checked results.

To model cost, explicitly supply both prices. No provider prices are assumed:

```bash
python -m evaluation.run_benchmark \
  --input-price-per-million 0 \
  --output-price-per-million 0
```

## Statistical contract

- Predictions match truth one-to-one by project, finding family, output, row, column, and comparison output. Message wording is ignored.
- Duplicates can match a truth finding only once; additional duplicates are false positives.
- Confidence intervals use 2,000 paired percentile bootstrap resamples of whole projects, preserving within-delivery dependence.
- Primary safety metrics include high-risk recall, false-positive findings per 100 current tables, clean-table specificity, issue-table miss rate, and unsafe auto-approval rate.
- `runtime_environment.json` contains machine-dependent wall-clock measurements and is deliberately excluded from reproducibility hashes. Dataset, predictions, scores, CSV files, and the report are deterministic for the same configuration.
- `benchmark_config.json` records the source-tree SHA-256 and the SHA-256 of the exact development dependency lock used for the run. `artifact_hashes.txt` pins the deterministic checked artifacts, so a reviewer can detect a stale or edited report independently of the runtime measurements.

## Test

```bash
python -m unittest discover -s evaluation/tests -v
```

The benchmark is an evaluation-harness demonstration. A real validation must replace the simulator with locked model calls and appropriately governed, expert-adjudicated documents.
