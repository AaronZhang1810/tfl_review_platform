# Synthetic TLF Review Benchmark Report

> **SYNTHETIC ENGINEERING BENCHMARK — all studies, tables, counts, and model behaviors are simulated. Results do not measure performance on real TLFs, do not constitute clinical validation, and do not establish regulatory fitness.**

Benchmark `synthetic-tlf-benchmark-v1` · seed `20260808` · 2,000 paired whole-project bootstrap resamples.

## Executive result

This benchmark exercises the evaluation plumbing and a seeded behavioral simulator. It does **not** measure Claude, real clinical documents, clinical accuracy, or production fitness.

| Configuration | Precision | Recall | F1 | High-risk recall | FP findings / 100 tables | Unsafe auto-approval |
|---|---:|---:|---:|---:|---:|---:|
| Rules-only (current structural checks) | 100.0% [100.0%, 100.0%] | 17.6% [13.0%, 22.4%] | 30.0% [23.0%, 36.6%] | 8.3% [4.1%, 12.8%] | 0.00 [0.00, 0.00] | 29.8% [27.7%, 32.0%] |
| LLM-only (seeded simulated proxy) | 75.4% [68.9%, 81.9%] | 74.1% [67.5%, 80.6%] | 74.8% [69.2%, 79.9%] | 77.5% [69.5%, 85.0%] | 8.20 [5.60, 11.20] | 12.0% [9.3%, 14.8%] |
| Hybrid (simulated proxy + deterministic guards) | 89.9% [85.1%, 94.2%] | 78.8% [71.5%, 85.0%] | 84.0% [78.7%, 88.2%] | 78.3% [70.4%, 85.7%] | 3.00 [1.60, 4.60] | 8.9% [6.5%, 11.4%] |

Intervals are descriptive paired percentile intervals under the simulator's fixed assumptions and deliberately stratified issue mix. They are not real-world prevalence or model-performance estimates.

## Dataset card

- 50 fictional projects and 1,000 pages; one table per page
- 170 planted issues across 17 finding families
- 120 high-risk planted issues and 680 clean check opportunities
- No figures, listings, scans, patient-level data, real studies, or real model calls

## What the guarded hybrid changes

The hybrid receives the same raw simulated predictions as the simulated model-only arm, then adds structural rules, arithmetic verification, deduplication, simulated self-check recovery, and an incomplete-coverage auto-approval gate.

Differences below are hybrid minus comparator. Lower is preferable for false-positive burden and unsafe auto-approval; higher is preferable for precision, recall, and F1.

| Paired comparison | Metric | Difference | 95% interval |
|---|---|---:|---:|
| hybrid simulated minus rules only | High-risk recall | 70.0% | [61.5%, 78.0%] |
| hybrid simulated minus rules only | Finding precision | -10.1% | [-14.9%, -5.8%] |
| hybrid simulated minus rules only | Finding recall | 61.2% | [55.3%, 66.9%] |
| hybrid simulated minus rules only | Finding F1 | 54.0% | [48.3%, 60.2%] |
| hybrid simulated minus rules only | False-positive findings / 100 current tables | 3.00 | [1.60, 4.60] |
| hybrid simulated minus rules only | Unsafe auto-approval rate | -20.9% | [-23.9%, -18.0%] |
| hybrid simulated minus llm only simulated | High-risk recall | 0.8% | [-1.8%, 3.7%] |
| hybrid simulated minus llm only simulated | Finding precision | 14.5% | [10.6%, 18.2%] |
| hybrid simulated minus llm only simulated | Finding recall | 4.7% | [1.2%, 8.6%] |
| hybrid simulated minus llm only simulated | Finding F1 | 9.2% | [6.6%, 12.0%] |
| hybrid simulated minus llm only simulated | False-positive findings / 100 current tables | -5.20 | [-7.20, -3.40] |
| hybrid simulated minus llm only simulated | Unsafe auto-approval rate | -3.1% | [-4.9%, -1.6%] |

## Guard diagnostics

- Arithmetic verification removed 17 unsupported simulated candidates.
- Deduplication removed 31 duplicate simulated candidates.
- Simulated page coverage was 98.6%; incomplete coverage blocks automatic clean status.
- Token counts, costs, and serial latency are modeled simulator outputs—not measured provider behavior.

## Evaluation contract and limitations

- Predictors see generated cases; only the scorer sees the separate truth labels.
- Matching is one-to-one by project, family, output, row, column, and comparison output. Duplicate predictions cannot reuse a truth match.
- The rules-only arm contains the platform's current structural families only; it is not a general non-AI TLF reviewer.
- Simulator probabilities are recorded assumptions, not fitted model characteristics.
- Results do not support regulatory validation, medical decisions, production deployment, or performance claims on real documents.

## Reproducibility

- Executable source SHA-256: `2285621a8d767f4df264143ca2168821b34855d2dbb5bcd33a628c94f37f98ea`
- Dependency-lock SHA-256: `4dff8cbfaad8fd4ceb05ea401768e8cfb14656dc71cd4b112b1a2bc509d55254`
- Dependency lock: `requirements-lock.txt`
- `artifact_hashes.txt` binds every deterministic checked artifact; machine-dependent runtime metadata is intentionally excluded.

Regenerate from the repository root with `python -m evaluation.run_benchmark`.

> **Permanent notice:** SYNTHETIC ENGINEERING BENCHMARK — all studies, tables, counts, and model behaviors are simulated. Results do not measure performance on real TLFs, do not constitute clinical validation, and do not establish regulatory fitness.
